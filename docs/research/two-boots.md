# Which boot meets the three constraints

Record of the combined read-only pass on 2026-10-13. Sources are `rhcos-ignition-only.md`, `bootc-containerfile-path.md`, and `nested-podman-path.md` in this directory.

The target must have node-exporter, FRR metrics, and FRR. FRR may be the host RPM or the OpenShift FRR image. Bytes on disk and a running process are separate.

## Result

Boot the bootc image. That host has a defined place for all three. Stock RHCOS plus Ignition does not. RHCOS running the Containerfile is not a path in this repo.

## Bootc

| Constraint | Bytes | Process |
| --- | --- | --- |
| FRR | Host `frr` RPM. Daemons file enables `bgpd` and `bfdd`. | `frr.service` waits for `/etc/frr/frr.conf`. These files do not write that conf. |
| FRR metrics | `COPY --from` the OpenShift FRR image to `/usr/bin/frr-metrics`. | `frr-metrics.service` runs that binary. `Containerfile.embed` does not. |
| Node-exporter | Build-time skopeo cache under `/usr/lib/containers-image-cache/node-exporter`. Boot-time copy to `node-exporter:embedded`. | Host `podman run` of that tag. The container does not run podman. |

The base is `FROM ${BOOTC_BASE}`, default `registry.redhat.io/rhel10/rhel-bootc`.

## RHCOS plus Ignition

| Constraint | Bytes | Process |
| --- | --- | --- |
| Node-exporter | `/var` after `podman pull` of the digest in `payload-images.env`. A reboot keeps it. | Host `podman run`. Needs `/usr/bin/podman` on that RHCOS build. Unchecked. Needs a registry secret. |
| FRR metrics | Inside a pulled image under `/var`, or not on the host. Ostree will not keep `/usr/bin/frr-metrics`. | 4.23 and 5.x exit without `KUBERNETES_SERVICE_HOST`. The 4.22 build can stay up and needs `vtysh`. The pinned digest's release was not resolved. |
| FRR | No Ignition install of the host RPM. Settled on 2026-10-07 as the bootc host RPM. | The other allowed source is `podman run` of the OpenShift FRR image. The Ignition plan forbids that unit. |

## Nested stack

No appliance file boots the bootc image as a container on RHCOS. Konflux "nested Podman" is a build limit. If someone ran the image with podman on RHCOS, SSH would stay on RHCOS, and the inner units would not start unless PID 1 inside the container is systemd. The Containerfile does not set that.

## What to stop

Stop planning an SSH to RHCOS that is supposed to have the bootc RPM layout. Stop treating the Konflux build note as a boot design. Stop using `Containerfile.embed` as the FRR metrics story. The settled metrics binary is `/usr/bin/frr-metrics` on the bootc image.

FRR config is outside both images. Neither path writes `frr.conf`.
