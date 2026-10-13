import base64
import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

import build


class PrototypeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ega-rhcos-test-")
        self.root = Path(self.temp.name)
        self.base = self.root / "base.raw"
        self.base.write_bytes(b"test base image")
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.payloads = tuple(self.make_payload(role) for role in build.ROLES)
        ignition = self.bundle / "appliance.ign"
        build.write_json(ignition, build.render_ignition(self.payloads, b"hostname prototype\n"))
        self.inventory = build.BuildInventory(
            1, "x86_64", str(self.base), build.sha256(self.base),
            build.sha256(ignition), self.payloads,
        )
        build.write_json(self.bundle / "inventory.json", asdict(self.inventory))

    def tearDown(self):
        self.temp.cleanup()

    def make_payload(self, role):
        config = json.dumps({"architecture": "amd64", "os": "linux", "role": role}).encode()
        image_id = "sha256:" + hashlib.sha256(config).hexdigest()
        path = self.bundle / f"{role}.tar"
        with tarfile.open(path, "w") as archive:
            for name, data in (
                ("config.json", config),
                ("manifest.json", json.dumps([{"Config": "config.json", "Layers": []}]).encode()),
            ):
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return build.Payload(role, "registry.example/" + role + "@sha256:" + "a" * 64,
                             image_id, path.name, build.sha256(path))

    def test_generated_ignition_runs_exactly_two_local_containers(self):
        config = json.loads((self.bundle / "appliance.ign").read_text())
        units = config["systemd"]["units"]
        workloads = [unit for unit in units if "podman run" in unit["contents"]]
        self.assertEqual([unit["name"] for unit in workloads],
                         ["ega-frr.service", "ega-node-exporter.service"])
        for unit in workloads:
            self.assertIn("--pull=never", unit["contents"])
            self.assertIn("Requires=ega-payload-load.service", unit["contents"])
        files = {item["path"]: base64.b64decode(item["contents"]["source"].split(",", 1)[1])
                 for item in config["storage"]["files"]}
        self.assertEqual(files["/etc/ega/frr-supervisor.sh"], (build.HERE / "frr-supervisor.sh").read_bytes())
        rows = files["/etc/ega/payloads.tsv"].decode().splitlines()
        self.assertEqual(len(rows), 2)
        for payload, row in zip(self.payloads, rows):
            self.assertEqual(row.split("\t"),
                             [payload.role, payload.archive, payload.archive_sha256, payload.image_id])
        self.assertNotIn("http", json.dumps(config))

    def test_verify_accepts_complete_bundle_without_running_commands(self):
        with mock.patch.object(build, "run", side_effect=AssertionError("external command")):
            self.assertEqual(build.verify_bundle(self.bundle), self.inventory)

    def test_verify_rejects_corrupt_archive(self):
        with (self.bundle / "frr.tar").open("ab") as stream:
            stream.write(b"damage")
        with self.assertRaisesRegex(ValueError, "archive checksum failed for frr"):
            build.verify_bundle(self.bundle)

    def test_verify_rejects_wrong_image_identity_even_with_correct_archive_hash(self):
        data = asdict(self.inventory)
        data["payloads"][0]["image_id"] = "sha256:" + "b" * 64
        (self.bundle / "inventory.json").write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "archive image identity failed for frr"):
            build.verify_bundle(self.bundle)

    def test_verify_rejects_duplicate_roles_and_path_escape(self):
        data = asdict(self.inventory)
        data["payloads"][1]["role"] = "frr"
        (self.bundle / "inventory.json").write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "exactly frr and node-exporter"):
            build.verify_bundle(self.bundle)
        data = asdict(self.inventory)
        data["payloads"][0]["archive"] = "../frr.tar"
        (self.bundle / "inventory.json").write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "unexpected payload archive path"):
            build.verify_bundle(self.bundle)

    def test_discover_var_rejects_ambiguous_state_roots(self):
        root = self.root / "mounted"
        for name in ("rhcos", "another"):
            (root / "ostree/deploy" / name / "var").mkdir(parents=True)
            (root / "ostree/deploy" / name / "deploy").mkdir()
        with self.assertRaisesRegex(ValueError, "exactly one OSTree state root"):
            build.discover_var(root)

    def test_cli_rejects_mutable_reference_before_download(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = build.main([
                "prepare", "--rhcos", str(self.base), "--rhcos-sha256", build.sha256(self.base),
                "--frr-image", "registry.example/frr:latest", "--node-image", self.payloads[1].source,
                "--output", str(self.root / "unused"),
            ])
        self.assertEqual(result, 1)
        self.assertIn("pinned", stderr.getvalue())
        self.assertFalse((self.root / "unused").exists())

    def test_prepare_refuses_existing_directory_before_registry_access(self):
        args = mock.Mock(rhcos=self.base, rhcos_sha256=build.sha256(self.base),
                         frr_image=self.payloads[0].source, node_image=self.payloads[1].source,
                         frr_config=build.HERE / "frr.conf", ssh_key=None, authfile=None,
                         output=self.bundle)
        with mock.patch.object(build, "require_tools"), mock.patch.object(build, "run") as command:
            with self.assertRaises(FileExistsError):
                build.prepare(args)
            command.assert_not_called()

    def test_prepare_produces_verifiable_archives_and_ignition(self):
        output = self.root / "prepared"
        args = mock.Mock(rhcos=self.base, rhcos_sha256=build.sha256(self.base),
                         frr_image=self.payloads[0].source, node_image=self.payloads[1].source,
                         frr_config=build.HERE / "frr.conf", ssh_key=None, authfile=None,
                         output=output)
        copied = []

        def registry_command(argv, *, capture=False):
            if argv[:2] == ["skopeo", "inspect"]:
                payload = next(item for item in self.payloads if argv[-1] == "docker://" + item.source)
                if "--raw" in argv:
                    return json.dumps({"config": {"digest": payload.image_id}})
                self.assertIn("--config", argv)
                return json.dumps({"architecture": "amd64", "os": "linux"})
            self.assertEqual(argv[:2], ["skopeo", "copy"])
            payload = next(item for item in self.payloads if argv[-2] == "docker://" + item.source)
            self.assertEqual(argv[-1], f"docker-archive:{output / payload.archive}:localhost/ega/{payload.role}:prototype")
            shutil.copyfile(self.bundle / payload.archive, output / payload.archive)
            copied.append(payload.role)

        with mock.patch.object(build, "require_tools"), \
                mock.patch.object(build, "run", registry_command), \
                contextlib.redirect_stdout(io.StringIO()):
            build.prepare(args)
        inventory = build.verify_bundle(output)
        self.assertEqual(inventory.payloads, self.payloads)
        self.assertEqual(copied, ["frr", "node-exporter"])
        self.assertEqual({path.name for path in output.iterdir()},
                         {"frr.tar", "node-exporter.tar", "appliance.ign", "inventory.json"})

    def test_disk_refuses_wrong_loop_backing_before_installer_or_detach(self):
        output = self.root / "disk"
        args = mock.Mock(bundle=self.bundle, output=output, size_gib=16, allow_unsigned_base=True)
        commands = []

        def disk_command(argv, *, capture=False):
            commands.append([str(item) for item in argv])
            if argv[:3] == ["losetup", "--find", "--show"]:
                return "/dev/loop7\n"
            if argv[:2] == ["losetup", "--json"]:
                return json.dumps({"loopdevices": [{"name": "/dev/loop7", "back-file": str(self.base)}]})
            self.fail(f"unexpected command {argv}")

        with mock.patch.object(build.os, "geteuid", return_value=0), \
                mock.patch.object(build, "require_tools"), \
                mock.patch.object(build, "run", disk_command), \
                self.assertRaisesRegex(RuntimeError, "loop ownership changed"):
            build.build_disk(args)
        self.assertTrue(commands)
        self.assertFalse(any(command[0] == "coreos-installer" for command in commands))
        self.assertFalse(any("--detach" in command for command in commands))
        self.assertEqual(self.base.read_bytes(), b"test base image")
        self.assertFalse((output / "appliance.raw").exists())

    def test_loader_accepts_bare_and_prefixed_ids_and_reuses_loaded_images(self):
        binary = self.root / "bin"
        binary.mkdir()
        podman = binary / "podman"
        podman.write_text("""#!/bin/bash
set -eu
case "$1 $2" in
    'image exists') grep -qxF "$3" "$STATE" ;;
    'load --input')
        case "$3" in frr.tar) id=$FRR_ID ;; node-exporter.tar) id=$NODE_ID ;; *) exit 2 ;; esac
        printf '%s\\n' "$id" >> "$STATE"
        printf '%s\\n' "$3" >> "$LOADS"
        ;;
    'image inspect')
        id=$5
        if [[ "$STYLE" == bare ]]; then id=${id#sha256:}; fi
        printf '%s\\n' "$id"
        ;;
    *) exit 2 ;;
esac
""")
        podman.chmod(0o755)
        state = self.root / "state"
        state.touch()
        loads = self.root / "loads"
        inventory = self.root / "payloads.tsv"
        inventory.write_text("".join(
            f"{item.role}\t{item.archive}\t{item.archive_sha256}\t{item.image_id}\n"
            for item in self.payloads
        ))
        env = {**os.environ, "PATH": str(binary) + ":" + os.environ["PATH"],
               "STATE": str(state), "LOADS": str(loads), "FRR_ID": self.payloads[0].image_id,
               "NODE_ID": self.payloads[1].image_id}
        for style in ("bare", "prefixed"):
            result = subprocess.run(["bash", build.HERE / "payload-load.sh", self.bundle, inventory],
                                    env={**env, "STYLE": style}, text=True, capture_output=True,
                                    check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(loads.read_text().splitlines(), ["frr.tar", "node-exporter.tar"])
        with (self.bundle / "frr.tar").open("ab") as stream:
            stream.write(b"damage")
        result = subprocess.run(["bash", build.HERE / "payload-load.sh", self.bundle, inventory],
                                env={**env, "STYLE": "bare"}, text=True, capture_output=True,
                                check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Archive checksum failed", result.stderr)
        self.assertEqual(loads.read_text().splitlines(), ["frr.tar", "node-exporter.tar"])

    def supervisor(self, metrics):
        worker = self.root / "worker.sh"
        worker.write_text("#!/bin/bash\ntrap 'exit 0' TERM\nwhile true; do sleep 1; done\n")
        worker.chmod(0o755)
        exporter = self.root / "exporter.sh"
        exporter.write_text("#!/bin/bash\n" + metrics + "\n")
        exporter.chmod(0o755)
        return subprocess.Popen([
            "bash", "-c", 'source "$1"; supervise "$2" "$3"', "supervisor-test",
            build.HERE / "frr-supervisor.sh", worker, exporter,
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)

    def test_supervisor_treats_clean_metrics_exit_as_failure(self):
        process = self.supervisor("exit 0")
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 1, (stdout, stderr))
        self.assertIn("exited with status 0", stderr)

    def test_supervisor_propagates_stop(self):
        process = self.supervisor("trap 'exit 0' TERM; while true; do sleep 1; done")
        time.sleep(0.2)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, (stdout, stderr))


if __name__ == "__main__":
    unittest.main()
