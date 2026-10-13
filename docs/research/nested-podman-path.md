# RHCOS running the bootc image

Record of a read-only pass on 2026-10-13. The question was whether RHCOS plus Ignition, used to run the current Containerfile image, ends in podman inside podman, and whether that stack exists in this repo.

## Result

No file describes that stack. RHCOS plus Ignition and the bootc image are two hosts kept side by side. Running the current image as a container on RHCOS would add one extra podman, and only for node-exporter.

## What was searched

- `image/rhel10/*.service`
- `evpn-on-cloud/image/Containerfile` and `evpn-on-cloud/image/README.md`
- `docs/rhcos-ignition-plan.md`
- `plans/context/evpn-aws/*.md` for "podman in podman", nested podman, and an RHCOS appliance host

No unit runs the appliance image on RHCOS. `image/rhcos/` has no files. `plans/context/evpn-aws/` has no RHCOS appliance host and no `podman run` of the appliance image. "Nested Podman" in `containerfile-refactor-spec.md` is a Konflux build limit. An outer pre-pull does not fill the nested engine's store. The `evpn-on-cloud` "podman-in-podman" line is the same kind of build-time limit.

## The two hosts that do exist

The RHCOS plan boots stock RHCOS. Ignition writes the sysctl file and `/etc/wireguard`. `evpn-image-pull.service` and `node-exporter.service` call `/usr/bin/podman` on that host. The plan does not `podman run` the appliance image.

The bootc path is a separate machine. FRR is the host RPM. `frr-metrics.service` is `/usr/bin/frr-metrics`. `payload-load.sh` is `skopeo copy`. `node-exporter.service` is the only `podman run`.

## If someone stacked them anyway

Assume `podman run` of the current rhel10 image on RHCOS, with systemd as PID 1 inside it.

1. RHCOS and its podman start the bootc container.
2. Inside that filesystem, `payload-load.sh` runs `skopeo`. That is a copy, not a second runtime.
3. The inner podman starts node-exporter. FRR and `frr-metrics` stay processes in the bootc filesystem.

That is two container runtimes. The prototype Containerfile would also put FRR in the inner podman. The current product file does not.

SSH to the RHCOS address lands on the outer ostree. The inner `frr` RPM and the node-exporter mount of `/` see the bootc container root, unless the outer run bind-mounts the RHCOS root. The RHCOS plan avoids that split. Its podman runs on the host the operator reaches.

The Containerfile does not set `/sbin/init` as PID 1. Without that, the inner units never start, so the second runtime does not appear. That PID 1 behavior was not tested.
