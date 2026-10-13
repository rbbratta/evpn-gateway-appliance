# Bootc Containerfile as the operating system

Record of a read-only pass on 2026-10-13. The question was whether booting `image/rhel10/Containerfile` requires podman inside podman, or whether that file is the operating system.

## Result

The Containerfile is the operating system. Booting it does not nest podman. The shape where RHCOS runs this image with podman, and the inner image runs podman again, is not what these files describe.

## What the file is

`image/rhel10/Containerfile` starts `FROM ${BOOTC_BASE}`. The default is `registry.redhat.io/rhel10/rhel-bootc`. The comment says CI overrides that with the public CentOS Stream bootc image. `plans/bootc-konflux-plan.md` treats `rhel10/rhel-bootc` as the bootable base, with kernel, firmware, and bootloader, and says not to use that base for a normal application container.

## How the three constraints are met

**FRR.** Host RPM `frr`. The daemons file is patched so `bgpd` and `bfdd` are yes. Drop-in `frr.service.d/wait-for-config.conf` has `ConditionPathExists=/etc/frr/frr.conf`. Nothing in these files writes `frr.conf`. The drop-in comment says Ansible does that later. Until that file exists, FRR stays down.

**FRR metrics.** Build-time `COPY --from` of `/frr-metrics` out of the OpenShift `frr-rhel9` image into `/usr/bin/frr-metrics`. `frr-metrics.service` runs that binary. No container. `Containerfile.embed` does not install this binary or this unit, so the embed alternate does not meet the metrics constraint.

**Node-exporter.** Build-time `skopeo copy` into `/usr/lib/containers-image-cache/node-exporter`. At boot, `payload-load.sh` copies that directory into containers-storage as `node-exporter:embedded`. `node-exporter.service` runs it with the host `podman run`, host network, and read-only mounts of `/proc`, `/sys`, and `/`. The listen address is `127.0.0.1:9100`. That container does not run podman.

## Boot order

1. bootc installs this image as the machine root. No unit starts the appliance image with `podman run`.
2. The booted OS enables `frr.service`, `frr-metrics.service`, `payload-load.service`, and `node-exporter.service`.
3. `payload-load.service` is a oneshot. The script walks `/usr/lib/containers-image-cache/*/`. For each directory that has `manifest.json` it runs `skopeo copy` into `containers-storage:<name>:embedded`. On this Containerfile that directory is only `node-exporter`. The script does not call `podman`.
4. `node-exporter.service` requires that oneshot, then runs `podman run` of `node-exporter:embedded`.
5. `frr.service` is the RPM unit. `frr-metrics.service` is `After=frr.service` and `ExecStart=/usr/bin/frr-metrics`.

Podman and skopeo are packages of the bootc OS. The only runtime container is node-exporter.

## Files read

- `image/rhel10/Containerfile`
- `image/rhel10/Containerfile.embed`
- `image/rhel10/payload-load.sh`
- `image/rhel10/payload-load.service`
- `image/rhel10/node-exporter.service`
- `image/rhel10/frr-metrics.service`
- `image/rhel10/payload-images.env`
- `plans/bootc-konflux-plan.md` (rhel-bootc base only)

## Notes

"Embeds other images" is true for node-exporter only on the primary Containerfile. The OpenShift FRR image is a build source for one binary.

`payload-load.service` lists `Before=frr-container.service`. This Containerfile never creates that unit. The embed file does.

The node-exporter cache is an OCI directory. It is not `podman load` of a tarball.

The worktree copy of `Containerfile.embed` has a broken `COPY` line. HEAD does not. CI `podman build` constructs the image. That is not the boot path.
