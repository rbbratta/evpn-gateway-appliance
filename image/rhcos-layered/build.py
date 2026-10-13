"""Build a layered RHCOS OCI image and convert it to an unbooted QEMU disk."""

import argparse
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEGACY = HERE.parent / "rhcos"
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("ega_rhcos_archives", LEGACY / "build.py")
archives = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = archives
spec.loader.exec_module(archives)
Payload = archives.Payload
GIB = 1024 ** 3
COSA_SOURCE = "258abe27117ce990d4286b7a60d97d7659e4651e"


@dataclass(frozen=True)
class BuildInputs:
    base: str
    frr: str
    node: str
    assembler: str
    version: str
    work_dir: Path


@dataclass(frozen=True)
class ResourceBudget:
    memory_bytes: int
    cpu_ids: tuple[int, ...]
    minimum_free_bytes: int
    disk_size_gib: int
    reserve_bytes: int = 4 * GIB
    vm_memory_mib: int = 4096


@dataclass(frozen=True)
class BaseContract:
    image_id: str
    osname: str
    image_config: dict
    unpacked_bytes: int


def unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def mounts():
    entries = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        fields = before.split()
        entries.append((Path(unescape_mount(fields[4])), after.split()[0],
                        Path(unescape_mount(fields[3]))))
    return entries


def filesystem(path, entries):
    path = path.resolve()
    matches = [entry for entry in entries if path.is_relative_to(entry[0])]
    if not matches:
        raise ValueError(f"filesystem type is unknown for {path}")
    return max(matches, key=lambda entry: len(entry[0].parts))[1]


def cgroup_available(entries):
    unified = [line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines()
               if line.startswith("0::")]
    mounted = [entry for entry in entries if entry[1] == "cgroup2"]
    if len(unified) != 1 or len(mounted) != 1:
        return {"known": False, "reason": "cgroup-v2 location is unavailable"}
    mount, _, root = mounted[0]
    membership = Path(unified[0])
    if not membership.is_relative_to(root):
        return {"known": False, "reason": "cgroup membership lies outside the visible mount"}
    current = mount / membership.relative_to(root)
    memory = []
    quotas = []
    try:
        controllers = (mount / "cgroup.controllers").read_text().split()
        if not {"memory", "cpu", "cpuset"}.issubset(controllers):
            raise ValueError("memory, CPU, and cpuset controllers are required")
        while True:
            if (current == mount and root == Path("/") and
                    not (current / "memory.max").exists() and not (current / "cpu.max").exists()):
                break
            maximum = (current / "memory.max").read_text().strip()
            usage = int((current / "memory.current").read_text())
            if maximum != "max":
                memory.append(max(0, int(maximum) - usage))
            quota, period = (current / "cpu.max").read_text().split()
            if quota != "max":
                quotas.append(int(quota) / int(period))
            if current == mount:
                break
            current = current.parent
    except (OSError, ValueError) as error:
        return {"known": False, "reason": str(error)}
    return {"known": True, "remaining_memory_bytes": min(memory) if memory else None,
            "cpu_quota": min(quotas) if quotas else None,
            "limit_scope": "visible cgroup and ancestors; null means no finite limit visible"}


def resource_snapshot(work_dir):
    entries = mounts()
    path = work_dir.resolve()
    while not path.exists():
        path = path.parent
    fs = filesystem(path, entries)
    info = os.statvfs(path)
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    available = int(memory["MemAvailable"].split()[0]) * 1024
    return {"filesystem": fs, "free_bytes": info.f_bavail * info.f_frsize,
            "host_available_memory_bytes": available, "cgroup": cgroup_available(entries),
            "cpu_affinity": sorted(os.sched_getaffinity(0))}


def check_resources(snapshot, budget):
    if snapshot["filesystem"] in {"tmpfs", "ramfs"}:
        raise ValueError("work and temporary data must be on a disk filesystem, not RAM")
    disk_floor = max(budget.minimum_free_bytes, (2 * budget.disk_size_gib + 4) * GIB)
    if snapshot["free_bytes"] < disk_floor:
        raise ValueError(f"insufficient disk space; require {disk_floor // GIB} GiB free")
    group = snapshot["cgroup"]
    if not group["known"]:
        raise ValueError("resource enforcement is unknown: " + group["reason"])
    usable = snapshot["host_available_memory_bytes"]
    remaining = group["remaining_memory_bytes"]
    if remaining is not None:
        usable = min(usable, remaining)
    if usable < budget.memory_bytes + budget.reserve_bytes:
        raise ValueError("insufficient available memory for the build budget and host reserve")
    if not set(budget.cpu_ids).issubset(snapshot["cpu_affinity"]):
        raise ValueError("requested CPU affinity is no longer available")
    quota = group["cpu_quota"]
    if quota is not None and quota < len(budget.cpu_ids):
        raise ValueError("requested CPUs exceed the visible cgroup CPU quota")


def validate_base(metadata, image_config, os_release, unpacked_bytes):
    labels = metadata.get("config", {}).get("Labels", {})
    if labels.get("containers.bootc") != "1" or labels.get("com.coreos.osname") != "rhcos":
        raise ValueError("base must identify itself as bootable RHCOS with its inherited labels")
    if not labels.get("org.opencontainers.image.version"):
        raise ValueError("base has no inherited OCI version")
    identity = {}
    for line in os_release.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            parsed = shlex.split(value)
            if len(parsed) == 1:
                identity[key] = parsed[0]
    if not (identity.get("ID") == "rhcos" or
            (identity.get("ID") == "rhel" and identity.get("VARIANT_ID") == "coreos")):
        raise ValueError("OS identity is not RHCOS")
    if (not isinstance(image_config.get("size"), int) or isinstance(image_config["size"], bool)
            or image_config["size"] <= 0 or not isinstance(image_config.get("extra-kargs"), list)
            or not all(isinstance(arg, str) for arg in image_config["extra-kargs"])):
        raise ValueError("base lacks compatible CoreOS Assembler image.json sizing and kernel arguments")
    return BaseContract(metadata["image_id"], "rhcos", image_config, unpacked_bytes)


def source_contract(manifest, config):
    if "manifests" in manifest or "config" not in manifest:
        raise ValueError("use a platform-specific image digest, not a multiarchitecture index")
    image_id = manifest["config"]["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("image configuration must use SHA256")
    if config.get("architecture") != "amd64" or config.get("os") != "linux":
        raise ValueError("all images must contain linux/amd64 content")
    return {**config, "image_id": image_id}


def make_context(work, disk_size_gib, base, payloads, frr_config):
    if tuple(item.role for item in payloads) != ("frr", "node-exporter"):
        raise ValueError("exactly the FRR and node-exporter payloads are required")
    context = work / "context"
    root = context / "rootfs"
    share = root / "usr/share/ega"
    (share / "payload").mkdir(parents=True)
    units = root / "usr/lib/systemd/system"
    wants = units / "multi-user.target.wants"
    wants.mkdir(parents=True)
    scripts = root / "usr/libexec/ega"
    scripts.mkdir(parents=True)
    for script in ("payload-load.sh", "frr-supervisor.sh"):
        shutil.copyfile(LEGACY / script, scripts / script)
        (scripts / script).chmod(0o755)
    for payload in payloads:
        os.link(work / "payload" / payload.archive, share / "payload" / payload.archive)
    images = {item.role: item.image_id for item in payloads}
    (share / "images.env").write_text(
        f"FRR_IMAGE_ID={images['frr']}\nNODE_EXPORTER_IMAGE_ID={images['node-exporter']}\n")
    (share / "payloads.tsv").write_text("".join(
        f"{item.role}\t{item.archive}\t{item.archive_sha256}\t{item.image_id}\n" for item in payloads))
    (share / "frr.conf").write_bytes(frr_config)
    archive_units = json.loads((LEGACY / "appliance.ign.in").read_text())["systemd"]["units"]
    for unit in archive_units:
        contents = unit["contents"]
        contents = contents.replace("RequiresMountsFor=/var/lib/ega/payload /var/lib/containers",
                                    "RequiresMountsFor=/var/lib/containers\nAfter=systemd-tmpfiles-setup.service")
        contents = contents.replace("ExecStartPre=/usr/sbin/restorecon -RF /var/lib/ega/payload\n", "")
        contents = contents.replace("/etc/ega/payload-load.sh",
                                    "/usr/libexec/ega/payload-load.sh /usr/share/ega/payload /usr/share/ega/payloads.tsv")
        contents = contents.replace("/etc/ega/images.env", "/usr/share/ega/images.env")
        contents = contents.replace("/etc/ega/frr-supervisor.sh", "/usr/libexec/ega/frr-supervisor.sh")
        (units / unit["name"]).write_text(contents)
        (wants / unit["name"]).symlink_to("../" + unit["name"])
    for relative, content in (
        ("usr/lib/tmpfiles.d/ega.conf", "d /etc/ega 0755 root root -\nC /etc/ega/frr.conf 0644 root root - /usr/share/ega/frr.conf\n"),
        ("usr/lib/sysctl.d/90-ega-forwarding.conf", "net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n"),
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    image_config = {**base.image_config, "size": disk_size_gib}
    metadata_path = root / "usr/share/coreos-assembler/image.json"
    metadata_path.parent.mkdir(parents=True)
    archives.write_json(metadata_path, image_config)
    shutil.copyfile(HERE / "Containerfile", context / "Containerfile")
    return context


def archive_unpacked_bytes(path):
    with tarfile.open(path, "r:*") as archive:
        return sum(member.size for member in archive if member.isfile())


def artifact_from_metadata(build_dir, metadata):
    artifact = metadata["images"]["qemu"]
    name = artifact["path"]
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".qcow2"):
        raise ValueError("COSA returned an unsafe qcow2 artifact path")
    path = build_dir / name
    if path.is_symlink() or not path.resolve().is_relative_to(build_dir.resolve()):
        raise ValueError("COSA artifact escapes its build directory")
    expected = archives.require_hash(artifact["sha256"])
    if archives.sha256(path) != expected:
        raise ValueError("COSA qcow2 checksum does not match its metadata")
    return path


def validate_qcow_info(info, disk_size_gib):
    if info.get("format") != "qcow2":
        raise ValueError("output is not qcow2")
    if any(info.get(key) for key in ("backing-filename", "full-backing-filename", "backing-filename-format")):
        raise ValueError("output depends on a backing file")
    if not 0 < info.get("virtual-size", 0) <= disk_size_gib * GIB:
        raise ValueError("qcow2 virtual size exceeds the requested disk budget")


class Builder:
    def __init__(self, inputs, budget, authfile=None):
        self.inputs = inputs
        self.budget = budget
        self.work = inputs.work_dir
        self.auth = ["--authfile", str(authfile)] if authfile else []
        self.environment = {**os.environ, "TMPDIR": str(self.work / "tmp")}
        self.podman = ["podman", "--remote=false", "--root", str(self.work / "storage"),
                       "--runroot", str(self.work / "runroot"), "--tmpdir", str(self.work / "podman-tmp")]

    def run(self, command, capture=False):
        check_resources(resource_snapshot(self.work), self.budget)
        command = [str(item) for item in command]
        with (self.work / "commands.jsonl").open("a") as log:
            log.write(json.dumps(command) + "\n")
        print("Running " + " ".join(command[:2]), flush=True)
        with (self.work / "build.log").open("a") as log:
            result = subprocess.run(command, env=self.environment, check=True, text=True,
                                    stdout=subprocess.PIPE if capture else log, stderr=log)
        return result.stdout

    def inspect_source(self, reference):
        source = "docker://" + reference
        manifest = json.loads(self.run(["skopeo", "inspect", *self.auth, "--raw", source], True))
        config = json.loads(self.run(["skopeo", "inspect", *self.auth, "--config", source], True))
        return source_contract(manifest, config)

    def pull(self, reference, metadata):
        self.run([*self.podman, "pull", *self.auth, reference])
        data = json.loads(self.run([*self.podman, "image", "inspect", reference], True))[0]
        if data["Id"].removeprefix("sha256:") != metadata["image_id"].removeprefix("sha256:"):
            raise ValueError("pulled image ID differs from the source configuration digest")
        return data

    def caps(self, build=False):
        args = [f"--memory={self.budget.memory_bytes}", f"--memory-swap={self.budget.memory_bytes}",
                "--cpuset-cpus=" + ",".join(str(cpu) for cpu in self.budget.cpu_ids)]
        if build:
            args += ["--cpu-period=100000", f"--cpu-quota={100000 * len(self.budget.cpu_ids)}", "--jobs=1"]
        else:
            args += [f"--cpus={len(self.budget.cpu_ids)}", "--pids-limit=2048"]
        return args

    def cosa(self, arguments, capture=False):
        command = [*self.podman, "run", "--rm", "--pull=never", "--network=none", "--privileged", "--cgroupns=private",
                   "--user=0", "--security-opt=label=disable", "--device=/dev/kvm", "--device=/dev/fuse",
                   *self.caps(), "-e", f"COSA_SUPERMIN_MEMORY={self.budget.vm_memory_mib}",
                   "-e", "RUNVM_CACHE_SIZE=20G", "-e", "TMPDIR=/var/tmp",
                   "-v", f"{self.work}/cosa:/srv", "-v", f"{self.work}/container-tmp:/tmp",
                   "-v", f"{self.work}/container-var-tmp:/var/tmp", "-w", "/srv"]
        if (self.work / "layered.oci").exists():
            command += ["-v", f"{self.work}/layered.oci:/input/layered.oci:ro"]
        return self.run([*command, self.inputs.assembler, *arguments], capture)

    def verify_enforcement(self):
        probe = "cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.swap.max /sys/fs/cgroup/cpu.max /sys/fs/cgroup/cpuset.cpus.effective"
        output = self.run([*self.podman, "run", "--rm", "--pull=never", "--network=none",
                           "--privileged", "--cgroupns=private", "--user=0", "--security-opt=label=disable",
                           *self.caps(), "--entrypoint=/bin/sh", self.inputs.assembler, "-c", probe], True)
        memory, swap, cpu, cpuset = output.strip().splitlines()
        quota, period = map(int, cpu.split())
        cpus = set()
        for part in cpuset.split(","):
            ends = list(map(int, part.split("-")))
            cpus.update(range(ends[0], ends[-1] + 1))
        if (int(memory) != self.budget.memory_bytes or int(swap) != 0 or
                quota / period != len(self.budget.cpu_ids) or cpus != set(self.budget.cpu_ids)):
            raise ValueError("container resource limits were not enforced as requested")

    def base_contract(self):
        metadata = self.inspect_source(self.inputs.base)
        labels = metadata.get("config", {}).get("Labels", {})
        if labels.get("containers.bootc") != "1" or labels.get("com.coreos.osname") != "rhcos":
            raise ValueError("base does not advertise the required inherited RHCOS bootable-image contract")
        image = self.pull(self.inputs.base, metadata)
        container = self.run([*self.podman, "create", "--pull=never", "--network=none",
                              "--entrypoint=/usr/bin/true", self.inputs.base], True).strip()
        if not re.fullmatch(r"[a-f0-9]{64}", container):
            raise ValueError("unexpected base inspection container ID")
        try:
            self.run([*self.podman, "cp", f"{container}:/usr/share/coreos-assembler/image.json", self.work / "base-image.json"])
            self.run([*self.podman, "cp", f"{container}:/usr/lib/os-release", self.work / "base-os-release"])
        finally:
            self.run([*self.podman, "rm", container])
        return validate_base(metadata, json.loads((self.work / "base-image.json").read_text()),
                             (self.work / "base-os-release").read_text(), image["Size"])

    def payload(self, role, reference):
        metadata = self.inspect_source(reference)
        path = self.work / "payload" / f"{role}.tar"
        self.run(["skopeo", "copy", *self.auth, "docker://" + reference,
                  f"docker-archive:{path}:localhost/ega/{role}:embedded"])
        check_resources(resource_snapshot(self.work), self.budget)
        if archives.archive_image_id(path) != metadata["image_id"]:
            raise ValueError(f"{role} archive configuration differs from the source image")
        return Payload(role, reference, metadata["image_id"], path.name, archives.sha256(path))

    def build(self, frr_config, ssh_key=None):
        base = self.base_contract()
        assembler = self.inspect_source(self.inputs.assembler)
        self.pull(self.inputs.assembler, assembler)
        self.verify_enforcement()
        self.cosa(["import", "--help"])
        self.cosa(["osbuild", "--help"])
        if "qemu" not in self.cosa(["osbuild", "--supported-platforms"], True).split():
            raise ValueError("selected assembler has no qemu platform")
        payloads = (self.payload("frr", self.inputs.frr), self.payload("node-exporter", self.inputs.node))
        payload_bytes = sum((self.work / "payload" / item.archive).stat().st_size for item in payloads)
        expanded = sum(archive_unpacked_bytes(self.work / "payload" / item.archive) for item in payloads)
        target_estimate = base.unpacked_bytes + payload_bytes + 2 * expanded + 8 * GIB
        if target_estimate > self.budget.disk_size_gib * GIB:
            raise ValueError("requested disk is too small for base, archives, expanded payloads, and reserve")
        working_estimate = (2 * self.budget.disk_size_gib + 4) * GIB + 3 * (base.unpacked_bytes + expanded)
        if resource_snapshot(self.work)["free_bytes"] < working_estimate:
            raise ValueError("remaining workspace cannot hold the estimated conversion and image copies")
        context = make_context(self.work, self.budget.disk_size_gib, base, payloads, frr_config)
        image_id_file = self.work / "layered-image-id"
        self.run([*self.podman, "build", "--pull=never", "--network=none", *self.caps(build=True),
                  "--iidfile", image_id_file, "--build-arg", f"RHCOS_BASE={self.inputs.base}",
                  "--build-arg", f"EGA_VERSION={self.inputs.version}", "-f", context / "Containerfile", context])
        image_id = image_id_file.read_text().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("builder returned an invalid image ID")
        layered = json.loads(self.run([*self.podman, "image", "inspect", image_id], True))[0]
        labels = layered.get("Labels", {})
        if (labels.get("containers.bootc") != "1" or labels.get("com.coreos.osname") != base.osname
                or labels.get("org.opencontainers.image.version") != self.inputs.version):
            raise ValueError("layered image did not retain required base labels and the selected version")
        self.run([*self.podman, "save", "--format=oci-archive", "--output", self.work / "layered.oci", image_id])
        self.cosa(["import", "--skip-prune", "oci-archive:/input/layered.oci"])
        build_dir = self.work / "cosa/builds" / self.inputs.version / "x86_64"
        metadata = json.loads((build_dir / "meta.json").read_text())
        imported_sha256 = archives.sha256(self.work / "layered.oci")
        if metadata["images"]["ostree"]["sha256"] != imported_sha256:
            raise ValueError("COSA imported different OCI bytes than the generated layered image")
        self.cosa(["osbuild", "--build", self.inputs.version, "qemu"])
        metadata = json.loads((build_dir / "meta.json").read_text())
        check_resources(resource_snapshot(self.work), self.budget)
        qcow = artifact_from_metadata(build_dir, metadata)
        qcow_info = json.loads(self.run(["qemu-img", "info", "--output=json", qcow], True))
        validate_qcow_info(qcow_info, self.budget.disk_size_gib)
        ignition = {"ignition": {"version": "3.4.0"}}
        if ssh_key:
            ignition["passwd"] = {"users": [{"name": "core", "sshAuthorizedKeys": [ssh_key]}]}
        archives.write_json(self.work / "firstboot.ign", ignition)
        evidence = {
            "inputs": {key: value for key, value in asdict(self.inputs).items() if key != "work_dir"},
            "budget": asdict(self.budget), "base": asdict(base),
            "payloads": [asdict(item) for item in payloads],
            "layered_image_id": image_id, "layered_archive_sha256": imported_sha256,
            "qcow2": str(qcow.relative_to(self.work)), "qcow2_sha256": archives.sha256(qcow),
            "estimated_target_bytes": target_estimate,
            "estimated_working_bytes": working_estimate,
            "inherited_container_imgref": base.image_config.get("container-imgref"),
            "cosa_source_contract": COSA_SOURCE, "guest_verified": False,
            "final_resources": resource_snapshot(self.work),
        }
        archives.write_json(self.work / "evidence.json", evidence)
        print(f"Created {qcow}; disconnected guest verification remains required")
        return qcow


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("base-image", "frr-image", "node-image", "assembler-image"):
        parser.add_argument("--" + option)
    parser.add_argument("--version")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--authfile", type=Path)
    parser.add_argument("--ssh-key", type=Path)
    parser.add_argument("--frr-config", type=Path, default=LEGACY / "frr.conf")
    parser.add_argument("--memory-gib", type=int, default=12)
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--minimum-free-gib", type=int, default=100)
    parser.add_argument("--disk-size-gib", type=int, default=48)
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        work = args.work_dir.resolve()
        if work.exists() or any(char in str(work) for char in ":,\n"):
            raise ValueError("choose a new work directory without colon, comma, or newline characters")
        affinity = sorted(os.sched_getaffinity(0))
        if not 1 <= args.cpus <= len(affinity):
            raise ValueError("CPU count exceeds the effective CPU affinity")
        if args.memory_gib < 8 or args.minimum_free_gib < 20 or not 24 <= args.disk_size_gib <= 256:
            raise ValueError("require at least 8 GiB memory, 20 GiB free-space floor, and a 24..256 GiB disk")
        budget = ResourceBudget(args.memory_gib * GIB, tuple(affinity[:args.cpus]),
                                args.minimum_free_gib * GIB, args.disk_size_gib)
        snapshot = resource_snapshot(work)
        if args.preflight_only:
            print(json.dumps({"resources": snapshot, "requested_budget": asdict(budget)}, indent=2))
        check_resources(snapshot, budget)
        if args.preflight_only:
            print("Resource preflight passed; no files, image downloads, or build processes were created")
            return 0
        if os.geteuid() != 0 or platform.machine() != "x86_64":
            raise ValueError("build requires a rootful x86_64 Linux builder")
        if not os.access("/dev/kvm", os.R_OK | os.W_OK) or not Path("/dev/fuse").exists():
            raise ValueError("build requires accessible /dev/kvm and /dev/fuse")
        references = [archives.require_image(value) for value in
                      (args.base_image, args.frr_image, args.node_image, args.assembler_image)]
        if not args.version or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", args.version):
            raise ValueError("version must be a simple nonempty build identifier")
        inputs = BuildInputs(*references, args.version, work)
        authfile = args.authfile.resolve(strict=True) if args.authfile else None
        frr_config = args.frr_config.read_bytes()
        if not frr_config.strip():
            raise ValueError("FRR configuration is empty")
        ssh_key = args.ssh_key.read_text().strip() if args.ssh_key else None
        if ssh_key and ("\n" in ssh_key or not ssh_key.startswith(("ssh-", "ecdsa-"))):
            raise ValueError("SSH key must be one public-key line")
        archives.require_tools("podman", "skopeo", "qemu-img")
        work.mkdir(parents=True, exist_ok=False)
        (work / ".gitignore").write_text("*\n")
        for directory in ("tmp", "storage", "runroot", "podman-tmp", "payload", "container-tmp",
                          "container-var-tmp", "cosa/builds", "cosa/cache", "cosa/tmp"):
            (work / directory).mkdir(parents=True, exist_ok=False)
        archives.write_json(work / "initial-resources.json", snapshot)
        Builder(inputs, budget, authfile).build(frr_config, ssh_key)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        if args.work_dir.exists():
            print(f"Retained work at {args.work_dir}; inspect build.log before retrying in a fresh directory", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
