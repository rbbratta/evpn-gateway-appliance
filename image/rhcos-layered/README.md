# Build the layered RHCOS proof of concept

This prototype layers two workload archives and host service definitions onto a
compatible RHCOS OCI image. CoreOS Assembler imports that local layered image and
produces an unbooted QEMU qcow2. The target runs two containers through host
Podman. FRR and `/frr-metrics` share one container. Node-exporter runs in the other.

All registry transfers happen on the connected builder. The target loads the
embedded archives from `/usr/share/ega/payload` and uses `--pull=never` with
verified image IDs. The layered OS image becomes the machine's operating system.
It is not an outer runtime container.

The files here provide an external build script. No OCI build, disk conversion,
or disconnected guest boot has been verified for this prototype. The
[OpenShift 4.22 layering documentation](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/machine_configuration/mco-coreos-layering)
describes building a derived RHCOS image. Its cluster deployment workflow does
not establish standalone appliance support or the QCOW2 conversion used here.

## Select compatible inputs

Use a rootful x86_64 Linux builder with cgroup v2, Podman, Skopeo, Python 3.10 or
later, `qemu-img`, and accessible `/dev/kvm` and `/dev/fuse`. The script uses fresh
container storage under its work directory and does not use the global store.

Supply four digest-pinned linux/amd64 image references. Use platform-specific
manifest digests because the script rejects multiarchitecture indexes.

- `RHCOS_IMAGE` is a bootable RHCOS container image. A matching OpenShift release
  can expose it through `oc adm release info --image-for=rhel-coreos RELEASE`.
  Do not substitute an arbitrary OS payload or ordinary RHEL image.
- `FRR_IMAGE` is an OpenShift FRR candidate with the standalone 4.22 exporter,
  `/usr/lib/frr/docker-start`, and the packaged `/sbin/tini` entrypoint.
- `NODE_IMAGE` contains `/bin/node_exporter` for linux/amd64.
- `COSA_IMAGE` is a CoreOS Assembler image with `import` and `osbuild qemu` support.

The base must retain `containers.bootc=1`, `com.coreos.osname=rhcos`, its original
OCI version, and `/usr/share/coreos-assembler/image.json`. The OS identity must
be `ID=rhcos`, or `ID=rhel` with `VARIANT_ID=coreos`. The script rejects bases
without this contract before it downloads either workload image. It does not
add compatibility labels or invent disk layout metadata to force acceptance.

The inspected conversion contract comes from CoreOS Assembler revision
`258abe27117ce990d4286b7a60d97d7659e4651e`. The selected assembler digest must
provide compatible commands. An older RHCOS or assembler image may fail these
checks. The script changes only the inherited image configuration's `size`
field. It retains the remaining boot and layout configuration.

## Check resources before building

Choose a new path on a disk filesystem. The read-only preflight does not create
files, start containers, or contact registries.

```sh
python3 image/rhcos-layered/build.py --preflight-only \
  --work-dir /srv/ega-poc-001
```

The default budget is 12 GiB for each build RUN container and the COSA container,
with swap disabled. COSA's helper VM gets 4 GiB inside that container budget.
The process uses two CPUs selected from the caller's effective affinity, with
both CPU affinity and CPU quota controls. Build stages run sequentially, and
Podman builds use one job.

The script requires another 4 GiB of available memory as a host reserve. It
checks host `MemAvailable` and remaining memory under visible cgroup ancestors.
Unknown cgroup enforcement causes a refusal. A root cgroup without finite limits
is reported separately from unavailable cgroup information. Before conversion,
a bounded container with the converter's privilege and cgroup settings confirms
the requested memory, swap, quota, and CPU set.

The initial disk floor is the larger of 100 GiB and twice the requested disk
size plus 4 GiB. After downloading images, the script also checks an estimate
that includes multiple copies of the base and expanded payloads. These estimates
are provisional, not measured maximum usage. Raising `--disk-size-gib` raises
the space check. The script checks free capacity and memory again before each
external command.

The work directory holds temporary files, archives, build context, container
storage, and COSA state. The script rejects tmpfs and ramfs, including paths
that resolve into them through symlinks. COSA's `/tmp` and `/var/tmp` point to
directories on that same disk. No RAM disk stores the build artifacts. Generated
work includes a `.gitignore` that excludes its contents.

The checks observe resources at that moment; they do not reserve capacity against
other jobs. Hard caps apply to build RUN containers and COSA. Host Skopeo
transfers, Podman archive operations, and streamed hashing run sequentially and
receive capacity checks, but do not have a separate global process limit. This
prototype does not guarantee that every host process stays within the container
budget.

For an aggregate limit, run the script in a dedicated build VM. For example,
assign 20 GiB RAM and two vCPUs, disable swap inside that VM, and retain the
script's default 12 GiB container budget. The host must have that capacity
available before starting the VM. Nested virtualization must expose `/dev/kvm`
to the builder. This bounds host-side commands as well as container processes.
A systemd scope around Python alone is not enough to promise an aggregate cap
because rootful Podman can create container cgroups outside that scope.

## Run the build on the external builder

Set the four image variables to your selected digest references. Keep the
registry authentication file outside the work directory and build context.

```sh
sudo python3 image/rhcos-layered/build.py \
  --base-image "$RHCOS_IMAGE" \
  --frr-image "$FRR_IMAGE" \
  --node-image "$NODE_IMAGE" \
  --assembler-image "$COSA_IMAGE" \
  --version ega-poc-001 \
  --authfile /secure/registry-auth.json \
  --ssh-key /srv/inputs/prototype.pub \
  --work-dir /srv/ega-poc-001 \
  --memory-gib 12 --cpus 2 \
  --disk-size-gib 48 --minimum-free-gib 100
```

`--frr-config PATH` replaces the demonstration configuration baked into the
layer. The default has no peers. It cannot demonstrate routing or useful peer
metrics. `--ssh-key` places a public key in the generated `firstboot.ign` sidecar.
No authentication file or registry credential is copied into the build context,
payload inventory, or guest.

The script inspects the base, pulls it into dedicated storage, and reads its
metadata through an unstarted container. It then checks the assembler and pulls
the two workload archives. The Containerfile preserves the base while adding
the archives, loader, supervisor, service units, and defaults. It runs
`bootc container lint`, as the 4.22 layering examples do. It does not install
packages. The generated context reuses the runtime scripts under
[`image/rhcos`](../rhcos/README.md).

After the layer builds, Podman saves a local OCI archive. The script initializes
fresh `builds`, `tmp`, and `cache` directories and runs these COSA commands inside
the bounded container.

```sh
cosa import --skip-prune oci-archive:/input/layered.oci
cosa osbuild --build ega-poc-001 qemu
```

The conversion uses a helper VM, but does not boot the delivered appliance. The
script checks that COSA imported the generated OCI archive's checksum. It obtains
the qcow2 path from that build's `meta.json`, checks its checksum, and rejects a
backing-file dependency. It writes `evidence.json` with the output path, input
digests, resource observations, and `guest_verified=false`.

The work directory also contains `layered.oci`, `firstboot.ign`, `build.log`, and
`commands.jsonl`. Failed builds retain their work. Use a fresh path for another
attempt. The script performs no global pruning and does not accept a physical
destination disk.

## Boot a disposable disconnected guest

Set `QCOW2` to the absolute output path printed by the builder. Create a writable
overlay on disk so the generated artifact remains unchanged. The QEMU user
network below permits the explicit SSH forwarding while restricting guest
outbound access. Test a production network's egress policy separately.

```sh
qemu-img create -f qcow2 -F qcow2 -b "$QCOW2" /srv/ega-guest.qcow2
qemu-system-x86_64 -enable-kvm -m 4096 -smp 2 -nographic \
  -drive if=virtio,format=qcow2,file=/srv/ega-guest.qcow2 \
  -fw_cfg name=opt/com.coreos/config,file=/srv/ega-poc-001/firstboot.ign \
  -nic user,model=virtio-net-pci,restrict=on,hostfwd=tcp:127.0.0.1:2222-:22
ssh -p 2222 core@127.0.0.1
```

Inside the guest, check the services and actual processes.

```sh
sudo systemctl status ega-payload-load ega-frr ega-node-exporter
sudo podman ps --format '{{.Names}}'
sudo podman exec ega-frr vtysh -c 'show version'
curl --fail http://127.0.0.1:7573/metrics
curl --fail http://127.0.0.1:9100/metrics
```

Require exactly `ega-frr` and `ega-node-exporter`. Verify node-exporter reports
the host. Restart and reboot the guest. Stop the FRR metrics process and verify
that its container restarts with both processes. On an isolated test network,
configure a peer and prove a BGP session, route installation, and changing BGP
or BFD metrics. A running service alone is not readiness evidence.

FRR remains privileged for this prototype. Node-exporter disables SELinux
container separation for its read-only host mount. Both exporters listen on
loopback. Production permissions, metrics access, and standalone RHCOS support
remain unresolved.

The evidence records inherited `container-imgref`. It may still name the
upstream base. Layer import establishes the disk's source bytes, not an approved
update path. Do not assume an upstream OS update preserves the appliance layer.
Current COSA may use bootc internally when the inherited image configuration
selects `bootc-install-to-fs`.

## Run the local tests

Use an existing disk-backed test directory. Tests use synthetic small archives
and mocked commands; they do not pull images or run privileged builders.

```sh
mkdir -p /srv/ega-unit-tests
TMPDIR=/srv/ega-unit-tests PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -s image/rhcos-layered -p test_build.py -v
```

## Source contracts

- [OpenShift 4.22 out-of-cluster layering source](https://github.com/openshift/openshift-docs/blob/enterprise-4.22/modules/coreos-layering-configuring.adoc) specifies a digest-pinned RHCOS base and shows the bootc lint check.
- [COSA import](https://github.com/coreos/coreos-assembler/blob/258abe27117ce990d4286b7a60d97d7659e4651e/src/cmd-import) imports layered OCI archives and requires the bootc and version labels.
- [COSA image extraction](https://github.com/coreos/coreos-assembler/blob/258abe27117ce990d4286b7a60d97d7659e4651e/src/cosalib/cmdlib.py) reads the inherited `image.json`.
- [COSA OSBuild](https://github.com/coreos/coreos-assembler/blob/258abe27117ce990d4286b7a60d97d7659e4651e/src/cmd-osbuild) produces the qemu artifact from the imported image.
- [COSA resource controls](https://github.com/coreos/coreos-assembler/blob/258abe27117ce990d4286b7a60d97d7659e4651e/src/cmdlib.sh) define helper VM memory and cache sizing.
- [Podman build](https://docs.podman.io/en/latest/markdown/podman-build.1.html) documents build memory, CPU period/quota, CPU affinity, and job controls.
