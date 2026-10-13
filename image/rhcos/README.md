# Build the disconnected RHCOS prototype

This prototype assembles an unbooted RHCOS disk with two workload images already
present as local archives. On first boot, Ignition configures host systemd and
Podman. Podman loads the archives and starts exactly two containers. The FRR
container runs FRR and `/frr-metrics` together. The other container runs
node-exporter.

The image downloads happen during `prepare` on a connected builder, through
`skopeo copy`. The target never pulls an image. Both container commands use
`--pull=never` and verified image IDs. Registry credentials remain on the builder.
This path does not use bootc, a product Containerfile, or nested Podman.

## Prepare the inputs

Use a Linux builder with Python 3.10 or later and Skopeo. Disk assembly also needs
root, CoreOS Installer, `losetup`, `lsblk`, `udevadm`, XFS mount support, `mount`,
`umount`, and `qemu-img`.

Supply an x86_64 RHCOS metal image accepted by CoreOS Installer, its independently
verified SHA256, and its adjacent `.sig` file. Supply platform-specific
linux/amd64 digest references for both containers. Manifest-list digests are
rejected. The script deliberately has no default base release or image digests.

Select an OpenShift 4.22 FRR candidate with `/usr/lib/frr/docker-start`,
`/sbin/tini`, Bash, and standalone `/frr-metrics`. The inspected source has those
paths. The selected registry digest still needs inspection and runtime testing.
Later FRR exporter revisions can require Kubernetes configuration.

Set `FRR_IMAGE` and `NODE_IMAGE` to your chosen digest references. Set `RHCOS_SHA256`
to the verified base checksum. Use fresh output directories for each attempt.
The existing [payload inventory](../payload-images.yaml) lists candidates, not
runtime-qualified inputs for this prototype. Do not use a newer exporter that
requires Kubernetes without a standalone implementation.

```sh
python3 image/rhcos/build.py prepare \
  --rhcos /srv/inputs/rhcos-metal.x86_64.raw.gz \
  --rhcos-sha256 "$RHCOS_SHA256" \
  --frr-image "$FRR_IMAGE" \
  --node-image "$NODE_IMAGE" \
  --authfile /secure/registry-auth.json \
  --ssh-key /srv/inputs/prototype.pub \
  --output /srv/ega-payload
python3 image/rhcos/build.py verify --bundle /srv/ega-payload
```

The output contains `frr.tar`, `node-exporter.tar`, `appliance.ign`, and
`inventory.json`. Keep the base image at its recorded absolute path for `verify`
and `disk`. `--frr-config PATH` replaces the demonstration configuration.
The supplied default has no peers and cannot prove routing or useful peer metrics.

The checked-in `appliance.ign.in` is a readable JSON template, not standalone
Ignition. `prepare` adds image IDs, expected archive hashes, FRR configuration,
and the contents of the two shell scripts. It emits the complete `appliance.ign`.
The chosen RHCOS release must accept Ignition 3.4.0 and provide the referenced
Podman and systemd behavior.

## Assemble the disk externally

Run this step on your external privileged builder. It has not been executed as
part of this prototype's local verification.

```sh
sudo python3 image/rhcos/build.py disk \
  --bundle /srv/ega-payload \
  --output /srv/ega-disk \
  --size-gib 24
```

The script creates its own sparse raw file and attaches that file to a loop
device. It verifies the backing-file mapping before calling CoreOS Installer.
It accepts no physical destination disk. CoreOS Installer runs with `--offline`,
the local base image, destination Ignition, and the `qemu` platform ID.

CoreOS Installer verifies the adjacent base signature. If your input has no
signature, `--allow-unsigned-base` explicitly permits its absence. The script
still checks the supplied SHA256. A checksum is not proof of the publisher.
CoreOS Installer continues to reject an invalid signature.

After installation, the script finds the loop disk's XFS root partition and the
single OSTree state root. It copies archives to that state root's
`var/lib/ega/payload`. It checks free space before copying. Increasing
`--size-gib` does not guarantee that an unbooted base's XFS filesystem grows.
If the existing root filesystem cannot fit the archives, assembly fails with
an explicit capacity error. Filesystem expansion is outside this prototype.

The output contains `appliance.raw`, `appliance.qcow2`, and `disk-evidence.json`
with artifact hashes. The disk remains unbooted, so first-boot identity and
Ignition state are not consumed. Interrupted builds retain a `work-*` directory
for diagnosis. Cleanup failures report the exact mount and loop to inspect.
Do not remove a work directory while it remains mounted.

## Verify the guest

Boot a disposable copy with registry and Internet egress blocked from its first
boot. The prototype uses the QEMU platform. AWS, VMware, and other platform
artifacts need separate packaging and qualification.

The loader runs target `restorecon` before it accesses the staged payload tree.
It checks archive hashes on every boot, loads missing image IDs, and verifies
their identities. FRR's supervisor preserves packaged daemon defaults and enables
zebra, bgpd, and bfdd. If either FRR or metrics exits, the supervisor stops the
other process and systemd restarts the container.

Run these checks inside the guest.

```sh
sudo systemctl status ega-payload-load ega-frr ega-node-exporter
sudo podman ps --format '{{.Names}}'
sudo podman exec ega-frr vtysh -c 'show version'
curl --fail http://127.0.0.1:7573/metrics
curl --fail http://127.0.0.1:9100/metrics
```

Confirm that `podman ps` lists exactly `ega-frr` and `ega-node-exporter`. Configure
a test peer and verify a BGP session, installed routes, and changing BGP or BFD
metrics. Confirm node-exporter reports the host's interfaces and filesystems.
Terminate `/frr-metrics` in the FRR container and verify the entire container
restarts. Reboot and repeat the checks. Corrupt an archive in a disposable guest
and confirm the next boot fails the loader and prevents both workloads starting.

The prototype deliberately runs FRR privileged. Node-exporter disables SELinux
container separation to observe the host and mounts the host root read-only.
It does not relabel the host root. Both metrics endpoints bind to loopback.
Production permissions and remote metric access need a separate design.

## Run local checks

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s image/rhcos -p test_build.py -v
bash -n image/rhcos/payload-load.sh image/rhcos/frr-supervisor.sh
python3 image/rhcos/build.py --help
```

Tests use synthetic archives and temporary directories. Set `TMPDIR` to a writable
filesystem with enough space if the default temporary filesystem is full. They
exercise generated units, archive integrity and identity checks, invalid CLI
inputs, refusal to reuse outputs, loop ownership, and supervisor failure and stop
behavior. The successful preparation test substitutes local fixtures for the
registry, so it does not establish authenticated registry compatibility.
They do not prove an RHCOS boot, SELinux labels, image compatibility, or routing.

RHCOS support outside OpenShift, workload redistribution, updates, and the exact
FRR exporter digest remain open product and runtime questions. The builder is
a prototype, not evidence that those questions are resolved.

## Source contracts

- [CoreOS Installer installation options](https://coreos.github.io/coreos-installer/cmd/install/) document local image and Ignition inputs and offline installation.
- [OSTree deployment layout](https://ostreedev.github.io/ostree/deployment/) documents `/ostree/deploy/<stateroot>/var`.
- [Skopeo copy](https://github.com/containers/skopeo/blob/main/docs/skopeo-copy.1.md) documents registry-to-archive copying. The inventory records the source digest, archive checksum, and configuration digest separately because archive conversion can change manifest representation.
- [The inspected FRR image definition](https://raw.githubusercontent.com/openshift/frr/1277b2387272397d3479d74d2d65112112f985b8/Dockerfile.openshift) and [exporter source](https://raw.githubusercontent.com/openshift/frr/1277b2387272397d3479d74d2d65112112f985b8/cmd/metrics/exporter.go) establish the candidate's paths and loopback metrics flags.
- [Node-exporter container guidance](https://github.com/prometheus/node_exporter#docker) describes host networking, PID visibility, and the host-root mount.
