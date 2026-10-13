# RHCOS plus Ignition, no Containerfile

Record of a read-only pass on 2026-10-13. The question was whether stock RHCOS plus Ignition, with no Containerfile, can boot a system that has node-exporter, FRR, and frr-metrics present and running.

The pass did not boot a guest. `podman` on the chosen RHCOS build is unchecked.

## Result

Stock RHCOS plus Ignition cannot boot all three running. The host `frr` RPM is not an Ignition install. The 4.23 and 5.x `/frr-metrics` process exits outside Kubernetes. Image bytes can land under `/var` after a pull, and only if `podman` is on that RHCOS build.

## What Ignition can place

The product `image/rhel10/Containerfile` places these artifacts. Ignition on stock RHCOS can place the `/etc` ones. It cannot place a path in the ostree `/usr`.

| Artifact | Ignition can place it |
| --- | --- |
| `dnf` set including `frr`, `podman`, and `skopeo` | No. Butane `fcos` has no package install. `--strict` rejected an `rpm-ostree` key. |
| Edited `/etc/frr/daemons` | The bytes, yes. The file does nothing without the `frr` RPM. |
| `/usr/bin/frr-metrics` copied from `openshift4/frr-rhel9` | No. Butane will compile the path. Ostree does not keep it. |
| `/etc/systemd/system/frr.service.d/wait-for-config.conf` | Yes. Useless without the RPM unit. |
| OCI dir `/usr/lib/containers-image-cache/node-exporter` | No. |
| `/etc/sysctl.d/99-ip-forward.conf` | Yes. |
| `/etc/wireguard` mode `0700` | Yes. The directory only. |
| `/usr/libexec/evpn-appliance/payload-load.sh` | No. |
| Unit files under `/etc/systemd/system` | The text, yes. `ExecStart` paths under `/usr` have nothing to run. |
| `systemctl enable frr.service` | No, unless that unit is already in the ostree commit. |

Ignition runs once in the initramfs and writes `/etc` and `/var`. A later `podman pull`, if the binary exists, stores layers under `/var/lib/containers`. A reboot keeps that tree. An Ignition file under `/usr` is not kept.

## Constraints

**Node-exporter.** One mechanism. An enabled unit runs `podman pull` of `registry.redhat.io/openshift4/ose-prometheus-node-exporter-rhel9@sha256:7d2884dc16f902e31b897d475d3e0dc09bc735df16b22544d0fefc4396a0ca48`, then `podman run` with the host mounts from `node-exporter.service`. Bytes live in `/var` after a successful pull. The process is that `podman run`. Both need `/usr/bin/podman` and a `registry.redhat.io` secret. The pull unit in the plan stays idle when `/etc/containers/auth.json` is absent.

**FRR metrics.** A pull of `openshift4/frr-rhel9@sha256:82fd7cc0123f6286bad46231a9939799ae79c30b9369047d2ffd109dacefa76e` stores `/frr-metrics` inside image layers under `/var`. That is not a host path. From 4.23 and on 5.x, `/frr-metrics` calls `rest.InClusterConfig()` and exits unless `KUBERNETES_SERVICE_HOST` is set. The 4.22 build listens on `127.0.0.1:7573` with no cluster and shells out to `/usr/bin/vtysh`. Which release digest `82fd7cc0…` is was not resolved. `frr-metrics.service` cannot point at `/usr/bin/frr-metrics` on RHCOS.

**FRR.** No mechanism installs the host `frr` RPM. Already settled on 2026-10-07, RHEL 10 FRR is the host `frr` RPM and `frr.service` on the bootc image, not the RHCOS ostree. The other allowed source is `podman run` of the OpenShift FRR image, privileged, host network, `/etc/frr` mounted, as `Containerfile.embed` does. The Ignition plan forbids `frr-container` and `FRR_IMAGE`. `/run` is a tmpfs, so a unit has to create `/run/frr` at start.

## Files read

- `docs/rhcos-ignition-plan.md`
- `image/rhel10/Containerfile`
- `image/rhel10/node-exporter.service`
- `image/rhel10/frr-metrics.service`
- `image/rhel10/payload-load.service`
- `image/rhel10/payload-load.sh`
- `image/rhel10/payload-images.env`
- `image/rhel10/Containerfile.embed` (worktree copy only)
- `plans/context/evpn-aws/agent-guide.md`
- `plans/context/evpn-aws/kickoff-decisions.md`
- `plans/context/evpn-aws/pipeline-spec.md`
- `plans/context/evpn-aws/source-evidence.md` sections 9 and 11

## Open

Whether `/usr/bin/podman` or `/usr/bin/skopeo` is in the RHCOS image is unchecked. Whether digest `sha256:82fd7cc0…` is a 4.22 build or a 4.23 or 5.x build is unchecked. Whether stock RHCOS can `rpm-ostree install` the RHEL 10 `frr` RPM is unchecked.
