import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("build.py")
spec = importlib.util.spec_from_file_location("ega_layered_builder", MODULE_PATH)
build = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = build
spec.loader.exec_module(build)


class LayeredTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(os.environ["TMPDIR"]).resolve(strict=True)
        if build.filesystem(temporary_root, build.mounts()) in {"tmpfs", "ramfs"}:
            self.fail("Set TMPDIR to an existing disk-backed directory")
        self.temporary = tempfile.TemporaryDirectory(prefix="layered-test-", dir=temporary_root)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.budget = build.ResourceBudget(12 * build.GIB, (0, 1), 100 * build.GIB, 48)
        self.inputs = build.BuildInputs(*[f"registry.example/{role}@sha256:" + "a" * 64
                                         for role in ("rhcos", "frr", "node", "cosa")], "poc-001", self.work)
        self.snapshot = {"filesystem": "btrfs", "free_bytes": 250 * build.GIB,
                         "host_available_memory_bytes": 40 * build.GIB, "cpu_affinity": [0, 1, 2, 3],
                         "cgroup": {"known": True, "remaining_memory_bytes": 32 * build.GIB,
                                    "cpu_quota": 4}}
        self.metadata = {"image_id": "sha256:" + "b" * 64, "architecture": "amd64", "os": "linux",
                         "config": {"Labels": {"containers.bootc": "1", "com.coreos.osname": "rhcos",
                                               "org.opencontainers.image.version": "base-001"}}}
        self.base = build.BaseContract(self.metadata["image_id"], "rhcos",
                                       {"size": 16, "extra-kargs": [], "container-imgref": "original-base"}, build.GIB)

    def tearDown(self):
        self.temporary.cleanup()

    def test_resource_rejections_happen_before_subprocesses(self):
        cases = [
            {**self.snapshot, "filesystem": "tmpfs"},
            {**self.snapshot, "free_bytes": 99 * build.GIB},
            {**self.snapshot, "host_available_memory_bytes": 15 * build.GIB},
            {**self.snapshot, "cgroup": {"known": False, "reason": "no visible controllers"}},
            {**self.snapshot, "cgroup": {"known": True, "remaining_memory_bytes": 15 * build.GIB, "cpu_quota": 4}},
        ]
        for snapshot in cases:
            with self.subTest(snapshot=snapshot), mock.patch.object(build, "resource_snapshot", return_value=snapshot), \
                    mock.patch.object(build.subprocess, "run") as command, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = build.main(["--preflight-only", "--work-dir", str(self.root / "not-created")])
                self.assertEqual(result, 1)
                command.assert_not_called()
                self.assertFalse((self.root / "not-created").exists())

    def test_disk_floor_scales_with_requested_disk(self):
        build.check_resources(self.snapshot, self.budget)
        with self.assertRaisesRegex(ValueError, "require 260 GiB"):
            build.check_resources(self.snapshot, replace(self.budget, disk_size_gib=128))

    def test_ram_filesystem_detection_resolves_symlinks(self):
        ram = self.root / "pretend-ram"
        ram.mkdir()
        link = self.root / "link"
        link.symlink_to(ram, target_is_directory=True)
        entries = [(self.root, "btrfs", Path("/")), (ram, "tmpfs", Path("/"))]
        self.assertEqual(build.filesystem(link / "new-work", entries), "tmpfs")

    def test_cgroup_remaining_accounts_for_ancestors(self):
        group = self.root / "cgroup"
        (group / "scope/job").mkdir(parents=True)
        (group / "cgroup.controllers").write_text("cpu memory cpuset\n")
        for directory, maximum, current, quota in (
            (group, "max", 1, "max 100000"),
            (group / "scope", str(16 * build.GIB), 3 * build.GIB, "200000 100000"),
            (group / "scope/job", str(24 * build.GIB), build.GIB, "400000 100000"),
        ):
            (directory / "memory.max").write_text(maximum)
            (directory / "memory.current").write_text(str(current))
            (directory / "cpu.max").write_text(quota)
        original = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == "/proc/self/cgroup":
                return "0::/scope/job\n"
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", read):
            result = build.cgroup_available([(group, "cgroup2", Path("/"))])
        self.assertEqual(result["remaining_memory_bytes"], 13 * build.GIB)
        self.assertEqual(result["cpu_quota"], 2)
        with self.assertRaisesRegex(ValueError, "available memory"):
            build.check_resources({**self.snapshot, "cgroup": result}, self.budget)

    def test_root_cgroup_without_limit_files_is_distinct_from_unknown(self):
        group = self.root / "cgroup"
        group.mkdir()
        (group / "cgroup.controllers").write_text("cpu memory cpuset\n")
        original = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == "/proc/self/cgroup":
                return "0::/\n"
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", read):
            result = build.cgroup_available([(group, "cgroup2", Path("/"))])
        self.assertTrue(result["known"])
        self.assertIsNone(result["remaining_memory_bytes"])
        build.check_resources({**self.snapshot, "cgroup": result}, self.budget)

    def test_base_accepts_both_rhcos_identities_and_rejects_generic_rhel(self):
        for identity in ("ID=rhcos\n", 'ID="rhel"\nVARIANT_ID=coreos\n'):
            result = build.validate_base(self.metadata, self.base.image_config, identity, build.GIB)
            self.assertEqual(result.osname, "rhcos")
        with self.assertRaisesRegex(ValueError, "OS identity"):
            build.validate_base(self.metadata, self.base.image_config, "ID=rhel\n", build.GIB)
        for missing in ("containers.bootc", "org.opencontainers.image.version"):
            metadata = json.loads(json.dumps(self.metadata))
            metadata["config"]["Labels"].pop(missing)
            with self.assertRaises(ValueError):
                build.validate_base(metadata, self.base.image_config, "ID=rhcos\n", build.GIB)

    def test_source_rejects_multiarch_and_wrong_architecture(self):
        with self.assertRaisesRegex(ValueError, "multiarchitecture"):
            build.source_contract({"manifests": []}, self.metadata)
        with self.assertRaisesRegex(ValueError, "linux/amd64"):
            build.source_contract({"config": {"digest": self.base.image_id}},
                                  {**self.metadata, "architecture": "arm64"})

    def payloads(self):
        (self.work / "payload").mkdir()
        results = []
        for role in ("frr", "node-exporter"):
            config = json.dumps({"architecture": "amd64", "os": "linux", "role": role}).encode()
            image_id = "sha256:" + hashlib.sha256(config).hexdigest()
            archive_path = self.work / "payload" / (role + ".tar")
            with tarfile.open(archive_path, "w") as archive:
                for name, data in (("config.json", config),
                                   ("manifest.json", b'[{"Config":"config.json","Layers":[]}]')):
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            self.assertEqual(build.archives.archive_image_id(archive_path), image_id)
            results.append(build.Payload(role, f"registry.example/{role}@sha256:" + "a" * 64,
                                         image_id, archive_path.name, build.archives.sha256(archive_path)))
        return tuple(results)

    def test_context_embeds_two_images_and_preserves_runtime_scripts(self):
        secret = self.root / "auth.json"
        secret.write_text('{"password":"do-not-copy-this-secret"}')
        payloads = self.payloads()
        context = build.make_context(self.work, 48, self.base, payloads, b"hostname test\n")
        root = context / "rootfs"
        self.assertEqual(sorted(path.name for path in (root / "usr/share/ega/payload").iterdir()),
                         ["frr.tar", "node-exporter.tar"])
        units = list((root / "usr/lib/systemd/system").glob("*.service"))
        wants = root / "usr/lib/systemd/system/multi-user.target.wants"
        expected_services = {"ega-payload-load.service", "ega-frr.service", "ega-node-exporter.service"}
        self.assertEqual({path.name for path in wants.iterdir()}, expected_services)
        for name in expected_services:
            self.assertTrue((wants / name).is_symlink())
            self.assertEqual(os.readlink(wants / name), "../" + name)
            self.assertEqual((wants / name).resolve(), wants.parent / name)
        workloads = [path.read_text() for path in units if "podman run" in path.read_text()]
        self.assertEqual(len(workloads), 2)
        for unit in workloads:
            self.assertIn("--pull=never", unit)
            self.assertIn("/usr/share/ega/images.env", unit)
        loader = (root / "usr/lib/systemd/system/ega-payload-load.service").read_text()
        self.assertNotIn("restorecon", loader)
        self.assertIn("/usr/share/ega/payload /usr/share/ega/payloads.tsv", loader)
        for script in ("payload-load.sh", "frr-supervisor.sh"):
            self.assertEqual((root / "usr/libexec/ega" / script).read_bytes(),
                             (build.LEGACY / script).read_bytes())
        image_config = json.loads((root / "usr/share/coreos-assembler/image.json").read_text())
        self.assertEqual(image_config, {**self.base.image_config, "size": 48})
        containerfile = (context / "Containerfile").read_text()
        self.assertIn("RUN bootc container lint", containerfile)
        self.assertLess(containerfile.index("COPY rootfs/ /"), containerfile.index("RUN bootc container lint"))
        for path in context.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"do-not-copy-this-secret", path.read_bytes())

    def test_artifact_validation_rejects_escape_corruption_and_backing_files(self):
        disk = self.work / "disk.qcow2"
        disk.write_bytes(b"synthetic disk")
        metadata = {"images": {"qemu": {"path": disk.name, "sha256": build.archives.sha256(disk)}}}
        self.assertEqual(build.artifact_from_metadata(self.work, metadata), disk)
        metadata["images"]["qemu"]["path"] = "../disk.qcow2"
        with self.assertRaisesRegex(ValueError, "unsafe"):
            build.artifact_from_metadata(self.work, metadata)
        metadata["images"]["qemu"]["path"] = disk.name
        disk.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            build.artifact_from_metadata(self.work, metadata)
        with self.assertRaisesRegex(ValueError, "backing file"):
            build.validate_qcow_info({"format": "qcow2", "virtual-size": 48 * build.GIB,
                                      "backing-filename": "other.qcow2"}, 48)
        build.validate_qcow_info({"format": "qcow2", "virtual-size": 48 * build.GIB}, 48)

    def test_conversion_uses_local_import_and_bounded_sequential_commands(self):
        runner = build.Builder(self.inputs, self.budget)
        (self.work / "layered.oci").write_bytes(b"synthetic archive")
        with mock.patch.object(runner, "run") as command:
            runner.cosa(["import", "--skip-prune", "oci-archive:/input/layered.oci"])
            runner.cosa(["osbuild", "--build", "poc-001", "qemu"])
        calls = [call.args[0] for call in command.call_args_list]
        self.assertEqual(calls[0][-3:], ["import", "--skip-prune", "oci-archive:/input/layered.oci"])
        self.assertEqual(calls[1][-4:], ["osbuild", "--build", "poc-001", "qemu"])
        for call in calls:
            self.assertIn("--pull=never", call)
            self.assertIn("--network=none", call)
            self.assertIn(f"--memory={12 * build.GIB}", call)
            self.assertIn(f"--memory-swap={12 * build.GIB}", call)
            self.assertIn("--cpuset-cpus=0,1", call)
            self.assertIn("--cpus=2", call)
            self.assertIn("COSA_SUPERMIN_MEMORY=4096", call)
            self.assertNotIn("--tmpfs", call)
        build_caps = runner.caps(build=True)
        self.assertIn("--cpu-quota=200000", build_caps)
        self.assertIn("--jobs=1", build_caps)
        self.assertFalse(any(flag.startswith("--cpus=") for flag in build_caps))

    def test_enforcement_refuses_mismatched_memory_swap_cpu_and_affinity(self):
        runner = build.Builder(self.inputs, self.budget)
        valid = [str(self.budget.memory_bytes), "0", "200000 100000", "0-1"]
        for index, invalid in ((0, str(8 * build.GIB)), (1, "1024"),
                               (2, "100000 100000"), (3, "0-3")):
            values = valid.copy()
            values[index] = invalid
            result = build.subprocess.CompletedProcess([], 0, stdout="\n".join(values) + "\n")
            with self.subTest(index=index), \
                    mock.patch.object(build, "resource_snapshot", return_value=self.snapshot), \
                    mock.patch.object(build.subprocess, "run", return_value=result), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaisesRegex(ValueError, "not enforced"):
                runner.verify_enforcement()

    def test_complete_builder_pipeline_emits_verified_local_artifacts_and_evidence(self):
        (self.work / "payload").mkdir()
        auth = self.root / "auth.json"
        auth.write_text('{"password":"pipeline-secret-must-stay-out"}')
        runner = build.Builder(self.inputs, self.budget, auth)
        source_configs = {
            self.inputs.base: {key: value for key, value in self.metadata.items() if key != "image_id"},
            self.inputs.frr: {"architecture": "amd64", "os": "linux", "role": "frr"},
            self.inputs.node: {"architecture": "amd64", "os": "linux", "role": "node-exporter"},
            self.inputs.assembler: {"architecture": "amd64", "os": "linux", "role": "assembler"},
        }
        source_bytes = {reference: json.dumps(config).encode() for reference, config in source_configs.items()}
        source_ids = {reference: "sha256:" + hashlib.sha256(data).hexdigest()
                      for reference, data in source_bytes.items()}
        layered_id = "sha256:" + "d" * 64
        inspection_id = "c" * 64
        disk_dir = self.work / "cosa/builds" / self.inputs.version / "x86_64"
        disk = disk_dir / "rhcos-poc-001-qemu.x86_64.qcow2"
        events = []

        def command_result(command, **kwargs):
            output = ""
            if command[0] == "skopeo":
                reference = command[-1].removeprefix("docker://")
                if command[1] == "inspect":
                    if "--raw" in command:
                        output = json.dumps({"config": {"digest": source_ids[reference]}})
                    else:
                        self.assertIn("--config", command)
                        output = source_bytes[reference].decode()
                else:
                    self.assertEqual(command[1], "copy")
                    reference = command[-2].removeprefix("docker://")
                    path = Path(command[-1].removeprefix("docker-archive:").split(":", 1)[0])
                    events.append("payload:" + source_configs[reference]["role"])
                    with tarfile.open(path, "w") as archive:
                        for name, data in (("config.json", source_bytes[reference]),
                                           ("manifest.json", b'[{"Config":"config.json","Layers":[]}]')):
                            member = tarfile.TarInfo(name)
                            member.size = len(data)
                            archive.addfile(member, io.BytesIO(data))
            elif command[0] == "podman":
                self.assertEqual(command[:len(runner.podman)], runner.podman)
                args = command[len(runner.podman):]
                if args[0] == "pull":
                    events.append("pull:base" if args[-1] == self.inputs.base else "pull:assembler")
                elif args[:2] == ["image", "inspect"]:
                    if args[-1] == layered_id:
                        output = json.dumps([{"Id": layered_id, "Labels": {
                            **self.metadata["config"]["Labels"],
                            "org.opencontainers.image.version": self.inputs.version}}])
                    else:
                        output = json.dumps([{"Id": source_ids[args[-1]].removeprefix("sha256:"),
                                              "Size": build.GIB}])
                elif args[0] == "create":
                    output = inspection_id
                elif args[0] == "cp":
                    if args[1].endswith("/image.json"):
                        Path(args[2]).write_text(json.dumps(self.base.image_config))
                    else:
                        self.assertTrue(args[1].endswith("/usr/lib/os-release"))
                        Path(args[2]).write_text("ID=rhel\nVARIANT_ID=coreos\n")
                elif args[0] == "rm":
                    self.assertEqual(args[1:], [inspection_id])
                elif args[0] == "build":
                    events.append("build")
                    context = Path(args[-1])
                    self.assertIn("RUN bootc container lint", (context / "Containerfile").read_text())
                    self.assertEqual((context / "rootfs/usr/share/ega/frr.conf").read_bytes(), b"hostname pipeline\n")
                    for path in context.rglob("*"):
                        if path.is_file():
                            self.assertNotIn(b"pipeline-secret-must-stay-out", path.read_bytes())
                    Path(args[args.index("--iidfile") + 1]).write_text(layered_id)
                elif args[0] == "save":
                    events.append("save")
                    self.assertEqual(args[-1], layered_id)
                    Path(args[args.index("--output") + 1]).write_bytes(b"synthetic layered OCI archive")
                elif args[0] == "run":
                    self.assertIn("--pull=never", args)
                    self.assertIn("--cgroupns=private", args)
                    self.assertIn("--user=0", args)
                    self.assertIn("--privileged", args)
                    self.assertIn(f"--memory={self.budget.memory_bytes}", args)
                    if "--entrypoint=/bin/sh" in args:
                        events.append("enforcement")
                        output = f"{self.budget.memory_bytes}\n0\n200000 100000\n0-1\n"
                    else:
                        operation = args[args.index(self.inputs.assembler) + 1:]
                        if operation == ["osbuild", "--supported-platforms"]:
                            output = "qemu metal"
                        elif operation[-1] == "--help":
                            output = "synthetic command help"
                        elif operation == ["import", "--skip-prune", "oci-archive:/input/layered.oci"]:
                            events.append("import")
                            disk_dir.mkdir(parents=True)
                            metadata = {"images": {"ostree": {
                                "sha256": build.archives.sha256(self.work / "layered.oci")}}}
                            (disk_dir / "meta.json").write_text(json.dumps(metadata))
                        else:
                            self.assertEqual(operation, ["osbuild", "--build", self.inputs.version, "qemu"])
                            events.append("osbuild")
                            metadata = json.loads((disk_dir / "meta.json").read_text())
                            disk.write_bytes(b"synthetic qcow2 disk")
                            metadata["images"]["qemu"] = {"path": disk.name, "sha256": build.archives.sha256(disk)}
                            (disk_dir / "meta.json").write_text(json.dumps(metadata))
                else:
                    self.fail(f"unexpected Podman command {args}")
            else:
                self.assertEqual(command, ["qemu-img", "info", "--output=json", str(disk)])
                events.append("qemu-info")
                output = json.dumps({"format": "qcow2", "virtual-size": 48 * build.GIB})
            self.assertEqual(kwargs["env"]["TMPDIR"], str(self.work / "tmp"))
            return build.subprocess.CompletedProcess(command, 0, stdout=output)

        key = "ssh-ed25519 AAAA-test-public-key pipeline"
        with mock.patch.object(build, "resource_snapshot", return_value=self.snapshot), \
                mock.patch.object(build.subprocess, "run", side_effect=command_result), \
                contextlib.redirect_stdout(io.StringIO()):
            result = runner.build(b"hostname pipeline\n", key)
        self.assertEqual(result, disk)
        self.assertEqual(events, ["pull:base", "pull:assembler", "enforcement", "payload:frr",
                                  "payload:node-exporter", "build", "save", "import", "osbuild", "qemu-info"])
        evidence = json.loads((self.work / "evidence.json").read_text())
        self.assertFalse(evidence["guest_verified"])
        self.assertEqual(evidence["layered_image_id"], layered_id)
        self.assertEqual(evidence["layered_archive_sha256"], build.archives.sha256(self.work / "layered.oci"))
        self.assertEqual(evidence["qcow2_sha256"], build.archives.sha256(disk))
        self.assertEqual(evidence["qcow2"], str(disk.relative_to(self.work)))
        self.assertEqual(evidence["inherited_container_imgref"], "original-base")
        self.assertEqual([item["role"] for item in evidence["payloads"]], ["frr", "node-exporter"])
        self.assertNotIn("pipeline-secret-must-stay-out", json.dumps(evidence))
        ignition = json.loads((self.work / "firstboot.ign").read_text())
        self.assertEqual(ignition["ignition"]["version"], "3.4.0")
        self.assertEqual(ignition["passwd"]["users"], [{"name": "core", "sshAuthorizedKeys": [key]}])


if __name__ == "__main__":
    unittest.main()
