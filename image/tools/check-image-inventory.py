#!/usr/bin/env python3
"""Gate image/ for floating tags and missing payload digests (source-audit §2).

Usage: image/tools/check-image-inventory.py [repo_root]
Exit 1 on :latest, unpinned runtime pulls in Containerfiles, or empty digests in payload-images.yaml.
"""
from __future__ import annotations

import pathlib
import re
import sys

try:
    import yaml
except ImportError:
    print("check-image-inventory: need PyYAML (python3-yaml)", file=sys.stderr)
    sys.exit(2)

LATEST = re.compile(r":latest\b")
RUNTIME_PULL = re.compile(
    r"(quay\.io|docker\.io|registry\.redhat\.io)/[^\s\"']+"
)


def repo_root(argv: list[str]) -> pathlib.Path:
    if len(argv) > 1:
        return pathlib.Path(argv[1]).resolve()
    return pathlib.Path(__file__).resolve().parents[2]


def check_containerfiles(image_dir: pathlib.Path) -> list[str]:
    errs: list[str] = []
    streams_path = image_dir / "streams.yaml"
    streams = {}
    if streams_path.is_file():
        streams = (yaml.safe_load(streams_path.read_text()) or {}).get("streams") or {}
    for stream_dir in sorted(p for p in image_dir.glob("rhel*") if p.is_dir()):
        for extra in ("Containerfile.ci", "Containerfile.layers"):
            path = stream_dir / extra
            if path.is_file():
                errs.append(f"{path}: remove it; pass BOOTC_BASE as a build-arg")
        cf = stream_dir / "Containerfile"
        if not cf.is_file():
            continue
        text = cf.read_text()
        spec = streams.get(stream_dir.name) or {}
        expected = (spec.get("bootcImage") or "").strip()
        ci_base = (spec.get("bootcImageCi") or "").strip()
        if "quay.io/centos-bootc/centos-bootc" not in ci_base:
            errs.append(
                f"{streams_path}: {stream_dir.name}.bootcImageCi must be quay.io/centos-bootc/centos-bootc"
            )
        if LATEST.search(text):
            errs.append(f"{cf}: contains :latest")
        arg_default = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("ARG BOOTC_BASE="):
                arg_default = stripped.split("=", 1)[1].strip()
            if stripped.startswith("FROM "):
                if stripped != "FROM ${BOOTC_BASE}":
                    errs.append(f"{cf}: FROM must be ${{BOOTC_BASE}}")
                if arg_default != expected:
                    errs.append(
                        f"{cf}: BOOTC_BASE default {arg_default!r} != streams.yaml bootcImage {expected!r}"
                    )
                if "@sha256:" not in arg_default:
                    errs.append(f"{cf}: bootc FROM not digest-pinned")
                continue
            if "quay.io/" in line or "docker.io/" in line or "frrouting" in line:
                errs.append(f"{cf}: community runtime image still referenced")
        if stream_dir.name == "rhel10":
            if "\n    frr \\\n" not in text:
                errs.append(f"{cf}: rhel10 FRR must be the host frr RPM")
            if "FRR_IMAGE" in text or "frr-container" in text:
                errs.append(f"{cf}: rhel10 still runs FRR as a container")
            if "cat > /etc/frr/daemons" in text:
                errs.append(f"{cf}: replaces packaged /etc/frr/daemons")
            if "s/^bgpd=no/bgpd=yes/" not in text or "s/^bfdd=no/bfdd=yes/" not in text:
                errs.append(
                    f"{cf}: bgpd and bfdd must be enabled in the packaged daemons file"
                )
            if "containers-image-cache/node-exporter" not in text:
                errs.append(f"{cf}: node-exporter must be embedded")
            if "podman pull" in text:
                errs.append(f"{cf}: runtime podman pull is not allowed")
            if "COPY --from=${FRR_METRICS_IMAGE} /frr-metrics /usr/bin/frr-metrics" not in text:
                errs.append(f"{cf}: frr-metrics must be copied from the pinned OCP image")
    return errs


def check_payload_yaml(path: pathlib.Path) -> list[str]:
    errs: list[str] = []
    if not path.is_file():
        return [f"missing {path}"]
    data = yaml.safe_load(path.read_text()) or {}
    for stream, images in data.items():
        if not isinstance(images, dict):
            continue
        for name, spec in images.items():
            if not isinstance(spec, dict):
                continue
            digest = (spec.get("digest") or "").strip()
            tag = (spec.get("tag") or "").strip()
            repo = (spec.get("repository") or "").strip()
            if not repo:
                continue
            if stream == "rhel10" and not tag:
                errs.append(f"{path}: {stream}.{name} missing tag (openshift5 not pinned yet)")
                continue
            if not digest:
                errs.append(f"{path}: {stream}.{name} missing digest (run image/tools/resolve-payload-digests.py)")
    return errs


def check_env_files(image_dir: pathlib.Path) -> list[str]:
    errs: list[str] = []
    for env in sorted(image_dir.glob("rhel*/payload-images.env")):
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if LATEST.search(line):
                errs.append(f"{env}: :latest in {line}")
            if "=" in line and "@sha256:" not in line.split("=", 1)[1]:
                errs.append(f"{env}: unpinned {line}")
    return errs


def main() -> int:
    root = repo_root(sys.argv)
    image_dir = root / "image"
    errs: list[str] = []
    errs.extend(check_containerfiles(image_dir))
    errs.extend(check_payload_yaml(image_dir / "payload-images.yaml"))
    errs.extend(check_env_files(image_dir))
    for e in errs:
        print(f"FAIL {e}")
    if errs:
        print(f"{len(errs)} image inventory check(s) failed")
        return 1
    print("image inventory OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
