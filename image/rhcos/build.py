"""Prepare local workload archives and an unbooted RHCOS prototype disk."""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROLES = ("frr", "node-exporter")
SHA256 = re.compile(r"[0-9a-f]{64}")
IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]+@sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class ImageInput:
    role: str
    reference: str


@dataclass(frozen=True)
class Payload:
    role: str
    source: str
    image_id: str
    archive: str
    archive_sha256: str


@dataclass(frozen=True)
class BuildInventory:
    version: int
    architecture: str
    rhcos_path: str
    rhcos_sha256: str
    ignition_sha256: str
    payloads: tuple[Payload, Payload]


def run(argv, *, capture=False):
    return subprocess.run(
        [str(item) for item in argv], check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_hash(value):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError("SHA256 must contain exactly 64 lowercase hexadecimal characters")
    return value


def require_image(value):
    if not isinstance(value, str) or not IMAGE.fullmatch(value):
        raise ValueError("image must be a registry reference pinned with @sha256:<64 hex characters>")
    if "/" not in value.split("@", 1)[0]:
        raise ValueError("image reference must include its registry and repository")
    return value


def require_tools(*names):
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise ValueError("missing required tools: " + ", ".join(missing))


def write_json(path, content):
    with Path(path).open("x") as stream:
        json.dump(content, stream, indent=2)
        stream.write("\n")


def ignition_file(path, contents, mode=0o644):
    return {
        "path": path, "mode": mode,
        "contents": {"source": "data:;base64," + base64.b64encode(contents).decode()},
    }


def render_ignition(payloads, frr_config, ssh_key=None):
    config = json.loads((HERE / "appliance.ign.in").read_text())
    images = {item.role: item.image_id for item in payloads}
    environment = f"FRR_IMAGE_ID={images['frr']}\nNODE_EXPORTER_IMAGE_ID={images['node-exporter']}\n"
    rows = "".join(
        f"{item.role}\t{item.archive}\t{item.archive_sha256}\t{item.image_id}\n"
        for item in payloads
    )
    files = config["storage"]["files"]
    for script in ("payload-load.sh", "frr-supervisor.sh"):
        files.append(ignition_file(f"/etc/ega/{script}", (HERE / script).read_bytes(), 0o755))
    for path, content in (
        ("/etc/ega/images.env", environment.encode()),
        ("/etc/ega/payloads.tsv", rows.encode()),
        ("/etc/ega/frr.conf", frr_config),
        ("/etc/sysctl.d/90-ega-forwarding.conf", b"net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n"),
    ):
        files.append(ignition_file(path, content))
    if ssh_key:
        config["passwd"] = {"users": [{"name": "core", "sshAuthorizedKeys": [ssh_key]}]}
    return config


def archive_image_id(path):
    with tarfile.open(path, "r:*") as archive:
        manifest_file = archive.extractfile("manifest.json")
        if manifest_file is None:
            raise ValueError(f"archive has no Docker manifest: {path}")
        entries = json.load(manifest_file)
        if len(entries) != 1:
            raise ValueError(f"archive must contain exactly one image: {path}")
        config_file = archive.extractfile(entries[0]["Config"])
        if config_file is None:
            raise ValueError(f"archive has no image configuration: {path}")
        return "sha256:" + hashlib.sha256(config_file.read()).hexdigest()


def prepare(args):
    base = args.rhcos.resolve(strict=True)
    expected = require_hash(args.rhcos_sha256)
    inputs = (
        ImageInput("frr", require_image(args.frr_image)),
        ImageInput("node-exporter", require_image(args.node_image)),
    )
    if not base.is_file() or sha256(base) != expected:
        raise ValueError("RHCOS base is not a regular file with the supplied SHA256")
    frr_config = args.frr_config.read_bytes()
    if not frr_config.strip():
        raise ValueError("FRR configuration is empty")
    ssh_key = args.ssh_key.read_text().strip() if args.ssh_key else None
    if ssh_key and ("\n" in ssh_key or not ssh_key.startswith(("ssh-", "ecdsa-"))):
        raise ValueError("SSH key must be one public-key line")
    auth = []
    if args.authfile:
        auth = ["--authfile", args.authfile.resolve(strict=True)]
    require_tools("skopeo")
    args.output.mkdir(parents=True, exist_ok=False)
    payloads = []
    for item in inputs:
        source = "docker://" + item.reference
        raw = run(["skopeo", "inspect", *auth, "--raw", source], capture=True)
        manifest = json.loads(raw)
        if "manifests" in manifest or "config" not in manifest:
            raise ValueError(f"{item.role} requires a platform-specific manifest digest, not an index")
        image_id = manifest["config"]["digest"]
        require_hash(image_id.removeprefix("sha256:"))
        if not image_id.startswith("sha256:"):
            raise ValueError("source image configuration must use SHA256")
        metadata = json.loads(run(["skopeo", "inspect", *auth, "--config", source], capture=True))
        if metadata.get("architecture") != "amd64" or metadata.get("os") != "linux":
            raise ValueError(f"{item.role} must contain linux/amd64 image content")
        archive = args.output / f"{item.role}.tar"
        run(["skopeo", "copy", *auth, source,
             f"docker-archive:{archive.resolve()}:localhost/ega/{item.role}:prototype"])
        if archive_image_id(archive) != image_id:
            raise ValueError(f"archive image configuration changed while copying {item.role}")
        payloads.append(Payload(item.role, item.reference, image_id, archive.name, sha256(archive)))
    ignition = args.output / "appliance.ign"
    write_json(ignition, render_ignition(payloads, frr_config, ssh_key))
    inventory = BuildInventory(1, "x86_64", str(base), expected, sha256(ignition), tuple(payloads))
    write_json(args.output / "inventory.json", asdict(inventory))
    print(f"Prepared offline payloads and Ignition in {args.output}")


def verify_bundle(bundle):
    data = json.loads((bundle / "inventory.json").read_text())
    if data.get("version") != 1 or data.get("architecture") != "x86_64":
        raise ValueError("unsupported inventory version or architecture")
    payloads = tuple(Payload(**item) for item in data["payloads"])
    if tuple(item.role for item in payloads) != ROLES:
        raise ValueError("inventory must contain exactly frr and node-exporter in that order")
    for item in payloads:
        require_image(item.source)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", item.image_id):
            raise ValueError("invalid payload image ID")
        if item.archive != f"{item.role}.tar":
            raise ValueError("unexpected payload archive path")
        if sha256(bundle / item.archive) != require_hash(item.archive_sha256):
            raise ValueError(f"archive checksum failed for {item.role}")
        if archive_image_id(bundle / item.archive) != item.image_id:
            raise ValueError(f"archive image identity failed for {item.role}")
    if sha256(bundle / "appliance.ign") != require_hash(data["ignition_sha256"]):
        raise ValueError("Ignition checksum failed")
    base = Path(data["rhcos_path"])
    if not base.is_absolute() or not base.is_file():
        raise ValueError("inventory RHCOS path must be an existing absolute file path")
    if sha256(base) != require_hash(data["rhcos_sha256"]):
        raise ValueError("RHCOS checksum failed")
    return BuildInventory(**{**data, "payloads": payloads})


def loop_backing_file(loop):
    data = json.loads(run(["losetup", "--json", "--output", "NAME,BACK-FILE", loop], capture=True))
    devices = data["loopdevices"]
    if len(devices) != 1 or devices[0]["name"] != loop:
        raise ValueError(f"cannot prove ownership of loop device {loop}")
    return Path(devices[0]["back-file"]).resolve()


def root_partition(loop):
    data = json.loads(run(["lsblk", "--json", "--paths", "--output", "NAME,TYPE,LABEL,FSTYPE", loop], capture=True))
    devices = data["blockdevices"]
    if len(devices) != 1 or devices[0]["name"] != loop:
        raise ValueError("unexpected loop partition listing")
    roots = [child for child in devices[0].get("children", [])
             if child["type"] == "part" and child.get("label") == "root"]
    if len(roots) != 1 or roots[0].get("fstype") != "xfs":
        raise ValueError("prototype requires exactly one XFS root partition on its loop device")
    return roots[0]["name"]


def discover_var(root):
    candidates = [path / "var" for path in (root / "ostree/deploy").iterdir()
                  if path.is_dir() and not path.is_symlink()
                  and (path / "var").is_dir() and not (path / "var").is_symlink()
                  and (path / "deploy").is_dir()]
    if len(candidates) != 1:
        raise ValueError("expected exactly one OSTree state root with its own var directory")
    return candidates[0]


def build_disk(args):
    if os.geteuid() != 0:
        raise ValueError("disk assembly requires root on an external Linux builder")
    if not 16 <= args.size_gib <= 1024:
        raise ValueError("disk size must be between 16 and 1024 GiB")
    inventory = verify_bundle(args.bundle)
    base = Path(inventory.rhcos_path)
    if not args.allow_unsigned_base and not Path(str(base) + ".sig").is_file():
        raise ValueError("base .sig file is required; --allow-unsigned-base explicitly permits its absence")
    require_tools("coreos-installer", "losetup", "lsblk", "udevadm", "mount", "umount", "qemu-img")
    args.output.mkdir(parents=True, exist_ok=False)
    work = Path(tempfile.mkdtemp(prefix="work-", dir=args.output))
    raw = work / "appliance.raw"
    mountpoint = work / "root"
    mountpoint.mkdir()
    with raw.open("xb") as stream:
        stream.truncate(args.size_gib * 1024 ** 3)
    loop = None
    mounted = False
    try:
        loop = run(["losetup", "--find", "--show", "--partscan", raw.resolve()], capture=True).strip()
        if not re.fullmatch(r"/dev/loop[0-9]+", loop) or loop_backing_file(loop) != raw.resolve():
            raise ValueError(f"loop backing file does not match {raw}")
        command = ["coreos-installer", "install", "--offline", "--image-file", base,
                   "--ignition-file", (args.bundle / "appliance.ign").resolve(),
                   "--platform", "qemu", "--architecture", "x86_64"]
        if args.allow_unsigned_base:
            command.append("--insecure")
        run([*command, loop])
        run(["udevadm", "settle"])
        partition = root_partition(loop)
        run(["mount", "-t", "xfs", "-o", "nouuid", partition, mountpoint])
        mounted = True
        target_var = discover_var(mountpoint)
        required = sum((args.bundle / item.archive).stat().st_size for item in inventory.payloads)
        if shutil.disk_usage(target_var).free < required + 64 * 1024 ** 2:
            raise ValueError("installed root filesystem cannot hold payload archives; use a larger compatible base filesystem")
        target = target_var / "lib/ega/payload"
        target.mkdir(parents=True, exist_ok=False)
        for item in inventory.payloads:
            shutil.copyfile(args.bundle / item.archive, target / item.archive)
            os.chmod(target / item.archive, 0o644)
        write_json(target / "payloads.json", [asdict(item) for item in inventory.payloads])
        os.sync()
    finally:
        if mounted:
            try:
                run(["umount", mountpoint])
            except BaseException as error:
                raise RuntimeError(f"unmount failed; retain {work}; mounted at {mountpoint}; loop {loop}") from error
        if loop:
            if loop_backing_file(loop) != raw.resolve():
                raise RuntimeError(f"loop ownership changed; retain {work}; inspect {loop} manually")
            try:
                run(["losetup", "--detach", loop])
            except BaseException as error:
                raise RuntimeError(f"detach failed; retain {work}; inspect {loop} manually") from error
    qcow = work / "appliance.qcow2"
    run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2", raw, qcow])
    evidence = {
        "architecture": inventory.architecture,
        "platform": "qemu",
        "source_inventory_sha256": sha256(args.bundle / "inventory.json"),
        "unsigned_base_permitted": args.allow_unsigned_base,
        "guest_boot_verified": False,
        "artifacts": {path.name: sha256(path) for path in (raw, qcow)},
    }
    raw.rename(args.output / raw.name)
    qcow.rename(args.output / qcow.name)
    write_json(args.output / "disk-evidence.json", evidence)
    print(f"Created unbooted raw and qcow2 disks in {args.output}; guest validation remains required")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="download both images on a connected builder")
    prep.add_argument("--rhcos", type=Path, required=True)
    prep.add_argument("--rhcos-sha256", required=True)
    prep.add_argument("--frr-image", required=True)
    prep.add_argument("--node-image", required=True)
    prep.add_argument("--authfile", type=Path)
    prep.add_argument("--ssh-key", type=Path)
    prep.add_argument("--frr-config", type=Path, default=HERE / "frr.conf")
    prep.add_argument("--output", type=Path, required=True)
    disk = commands.add_parser("disk", help="assemble a disk using local inputs and a scratch loop device")
    disk.add_argument("--bundle", type=Path, required=True)
    disk.add_argument("--output", type=Path, required=True)
    disk.add_argument("--size-gib", type=int, default=24)
    disk.add_argument("--allow-unsigned-base", action="store_true")
    verify = commands.add_parser("verify", help="check local base, archives, image identities, and Ignition")
    verify.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args)
        elif args.command == "disk":
            build_disk(args)
        else:
            verify_bundle(args.bundle)
            print("Verified local base, both payload archives, image IDs, and Ignition")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
