# RHCOS and Ignition appliance redesign plan

Proposal, 2026-10-13. Based on branch `bootc-image-import` at
`67524c74561280138e12fa55b4439ac131f7f8f7` and the current working-tree research.
This document proposes an architecture and its validation work. It does not
record an approved product decision or a successful appliance boot.

The follow-up request permits a build-time Containerfile. The
[layered RHCOS proof of concept](../image/rhcos-layered/README.md) packages both
workload archives in the OS image and uses CoreOS Assembler to generate QCOW2.
It adds disk-backed storage and resource preflight checks. That path requires a
compatible RHCOS OCI base and external build and boot qualification. The original
Ignition-and-disk-staging prototype described below remains available unchanged.

## Recommendation

Use RHCOS as the operating system. Use Ignition to configure host systemd
services. Have those services run the OpenShift FRR image and the node-exporter
image directly through the RHCOS host's Podman. Run `/frr-metrics` alongside FRR
inside the FRR container. A supervisor owns both processes and fails the
container if either process exits.

This is technically plausible from the documented components and inspected
source. It satisfies the three requested constraints without a product
Containerfile, bootc, or Podman inside Podman, provided the selected images pass
the runtime checks below. There are two workload images and exactly two
containers. This incorporates the follow-up request to keep FRR and metrics
together and to require disconnected operation from the first boot.

For the preferred self-contained disk, a builder must stage the workload image
archives before the customer's first boot. Ignition configures the installed
system. It does not generate a disk image or automatically embed container
images. CoreOS Installer supplies the OS installation mechanism. The
[local prototype](../image/rhcos/README.md) implements archive preparation,
Ignition generation, and offline disk assembly. Its disk output still needs a
disconnected guest boot test.

The principal unresolved issues are the exact FRR metrics build and standalone
RHCOS product support. Neither issue establishes a need for nested Podman.

## The target and its three constraints

The proposed target has this structure:

```text
RHCOS host
  systemd
    ega-payload-load.service
      loads two local image archives into the host Podman store
    ega-frr.service
      host Podman runs FRR and /frr-metrics in one supervised container
    ega-node-exporter.service
      host Podman runs the node-exporter image
```

The system reached by SSH owns these services and their container storage.
No service runs the existing appliance image as an outer application container.

| Required payload | Proposed location on the target | Running process | Acceptance evidence |
| --- | --- | --- | --- |
| Node-exporter image | Archive under `/var/lib/ega/payload`, then the host Podman store | Node-exporter container | The selected image is local, and its metrics describe the RHCOS host. |
| FRR metrics | `/frr-metrics` and `vtysh` inside the local OpenShift FRR image | Metrics process inside the FRR container | A scrape contains FRR BGP and BFD data and changes when a test peer changes state. |
| FRR, from an exact RPM or the OpenShift image | The local OpenShift FRR image | FRR container in the host network namespace | FRR reports the expected version, establishes a test BGP session, and installs the expected routes. |

This proposal chooses the image alternative that the request permits. It does
not require a host FRR RPM. It also interprets metrics being on the target as a
local, running exporter, without requiring `/usr/bin/frr-metrics` on the host.

## What the existing research proves

The earlier [Ignition-only research](research/rhcos-ignition-only.md) correctly
finds that Ignition cannot perform the current Containerfile's package
installation. Its broader conclusion that RHCOS cannot run all three payloads
does not follow. That conclusion also assumes the old plan's prohibition on an
FRR container and its host `/usr/bin/frr-metrics` layout.

The [existing Ignition plan](rhcos-ignition-plan.md) deliberately excludes FRR
and metrics. It is a node-exporter provisioning experiment, not a candidate that
meets this request. This proposal replaces that experiment's scope for the next
implementation. It leaves the earlier document as a record of the prior plan.

The [bootc research](research/bootc-containerfile-path.md) and
[nested-Podman research](research/nested-podman-path.md) establish another useful
distinction. A bootc image installed as the operating system runs Podman on the
host. Embedding image bytes in that OS does not create nested containers.
Running that entire OS image as a container on RHCOS, then starting its embedded
workloads through an inner Podman, would create the extra runtime layer.

The actual files also differ. The primary
[Containerfile](../image/rhel10/Containerfile) installs host FRR, copies a metrics
binary, and embeds node-exporter. The
[embed alternate](../image/rhel10/Containerfile.embed) embeds both workload
images but does not start the metrics exporter. Neither file is needed in the
proposed RHCOS build path.

The RHEL 10 host-RPM row in the
[decision list](../plans/context/evpn-aws/kickoff-decisions.md) describes the
existing RHEL 10 design. It carries a date but no named decision owner. Under the
repository's owner-and-date rule, it does not establish an approved prohibition
on the RHCOS image alternative. This proposal does not edit that row.

## Evidence and limits

Red Hat's RHCOS documentation lists Podman and Skopeo, writable `/etc` and `/var`,
and container storage under `/var/lib/containers`. It describes `/usr` as
read-only. It also limits RHCOS support to its use as an OpenShift component and
discourages direct container-tool use outside debugging. These are documented
capabilities and support boundaries, not verification of a particular appliance
image. See [RHCOS architecture](https://docs.redhat.com/en/documentation/openshift_container_platform/4.21/html/architecture/architecture-rhcos).

[Ignition](https://coreos.github.io/ignition/getting-started/) applies first-boot
configuration and can enable systemd services. Those services subsequently run
Podman. A package transaction could be invoked by a service, but that would add
package repositories, an OS deployment change, and potentially a reboot. Plain
Ignition does not install an FRR RPM into the stock RHCOS deployment.

The [OpenShift FRR image source](https://raw.githubusercontent.com/openshift/frr/1277b2387272397d3479d74d2d65112112f985b8/Dockerfile.openshift)
contains FRR and `/frr-metrics`. At the inspected 4.22 revision,
[the exporter](https://raw.githubusercontent.com/openshift/frr/1277b2387272397d3479d74d2d65112112f985b8/cmd/metrics/exporter.go)
starts an HTTP server without Kubernetes configuration and registers BGP and BFD
collectors. The inspected
[5.1 exporter](https://raw.githubusercontent.com/openshift/frr/567c9101668ad51d96feb15ff121d69d6256fc1a/cmd/metrics/exporter.go)
unconditionally calls `rest.InClusterConfig()` when it constructs its metrics
handler. Changing the host OS does not remove that dependency.

The [current payload inventory](../image/payload-images.yaml) associates its FRR
digest with a 4.22 tag. Both existing image digest inspections returned registry
authentication errors during this investigation. The tag-to-digest mapping,
packaged executables, platform, and actual standalone startup remain unverified.
The branch source is evidence for a candidate, not proof about those registry
bytes. No RHCOS guest was booted for this plan.

## Image assembly and first boot

The preferred output is a disk containing the stock RHCOS OS deployment,
destination Ignition, a payload inventory, and the two workload image archives.
The OS content under `/usr` remains the supplied RHCOS content. The disk as a
whole is a customized appliance artifact.

1. The builder selects a specific RHCOS release, architecture, and target
   platform. It records the source artifact, checksum, and verification method.
   It resolves both workload images to platform-specific digests and creates
   local archives. Registry credentials exist only in the build environment.
2. The Python prototype renders a readable JSON template into raw Ignition
   accepted by the selected RHCOS release. The output is not a `MachineConfig`
   object. The configuration defines host services, forwarding settings, and
   appliance configuration paths.
3. CoreOS Installer installs the pinned RHCOS image onto a disposable build disk
   and embeds destination Ignition. Its documented options include a local image,
   an Ignition file, and the destination platform ID. See
   [CoreOS Installer install](https://coreos.github.io/coreos-installer/cmd/install/).
4. A builder step stages the archives and their inventory in the installed
   system's `/var/lib/ega/payload`. It discovers the selected deployment's actual
   filesystem layout and applies the required ownership. The target restores
   SELinux labels using its own policy before loading the archives.
   It does not assume that a mounted partition's top-level `/var` is the deployed
   system's `/var`. The prototype requires exactly one OSTree state root. A
   [post-install hook](https://coreos.github.io/coreos-installer/customizing-install/)
   provides an integration point, not a ready-made archive packager.
5. The builder exports the unbooted destination disk. Validation boots disposable
   copies. The published disk must not contain a validation VM's machine ID,
   host keys, credentials, or consumed first-boot state.
6. On the target's first boot, Ignition writes the configuration and enables the
   host units. A loader verifies archive checksums, loads them with host Podman,
   and verifies the resulting image identities. The services then start with
   `--pull=never`. Later boots reuse verified local images or reload the archives.

The builder stages archives outside Podman's storage database. The target's own
Podman creates its storage records during loading. The builder does not copy a
running build host's `/var/lib/containers` into the appliance disk.

The archive format must work with the selected Podman version. Podman's
[load command](https://docs.podman.io/en/latest/markdown/podman-load.1.html)
supports loading image archives. The implementation must preserve or explicitly
verify the relationship between source manifest digest, archive checksum, and
local image ID. A locally assigned tag alone is insufficient evidence.

A first-boot registry-pull variant is useful for an early experiment. It also
avoids bootc and the Containerfile. However, it requires target registry access
and does not deliver image bytes in the initial disk. It is not the default for
the requested self-contained appliance.

If offline disk staging is unsuitable, an installer plus local payload medium
is the fallback to test. The target can load archives from that medium. That
changes the delivery artifact and must be described as such. Embedding Ignition
in a live ISO configures the live boot unless destination configuration is
explicitly supplied. An ISO, a qcow2 disk, and an AMI are different artifacts.

## Runtime contracts

Plain systemd services that invoke host Podman are the initial proposal. Quadlet
can replace those definitions if the selected RHCOS Podman version supports the
required features. Neither choice changes the runtime architecture.

The loader is required by every workload service. Missing or corrupt payloads
produce a failed readiness result. Missing registry credentials must not turn a
required payload into a silently skipped service. The local-archive path needs
no registry credentials on the target.

FRR uses host networking so that its routing operations affect the appliance.
The pinned image's packaged startup command must be inspected and tested.
The upstream [startup script](https://raw.githubusercontent.com/openshift/frr/1277b2387272397d3479d74d2d65112112f985b8/docker-start)
starts `watchfrr` with the configured daemon list. An empty mount over `/etc/frr`
would hide packaged configuration. The preparation step must preserve the
complete packaged daemon defaults, enable the required daemons, and supply a
valid `frr.conf` before FRR starts. A skipped FRR service is not a passing boot.

The FRR service overrides the image's startup command with a mounted supervisor
under its packaged init process. That supervisor starts FRR and `/frr-metrics`,
terminates the remaining process if either exits, and returns a failure so
systemd restarts the container. The same image supplies `vtysh` and its userspace
dependencies. The processes share FRR's socket directory inside the container.
There is no cross-container socket mount or separate metrics container.

The container recreates `/run/frr` with the required ownership at startup.
Guest acceptance must check that `vtysh` can query FRR before accepting metrics.
Starting the supervised processes does not establish that readiness.
The prototype uses privileged FRR execution. Qualification still needs to
determine the minimum capabilities and SELinux policy. That privilege choice is
not evidence of the minimum required permissions. See
[Podman run](https://docs.podman.io/en/latest/markdown/podman-run.1.html).

Node-exporter gets host networking, host PID visibility where required, and
read-only host filesystem mounts with matching `--path.*` arguments. The
[upstream container guidance](https://raw.githubusercontent.com/prometheus/node_exporter/master/README.md)
explains how to observe the host. The test must identify the actual host's
interfaces and filesystem mounts. It must not relabel the entire host root to
make a bind mount work.

The initial experiment binds metrics to host loopback. Production collection
needs an explicit reachable address and access policy. The chosen image's bind,
port, and authentication flags are part of the inventory's runtime contract.
Changing an image digest requires repeating the startup and scrape tests.

## Alternatives

| Path | Meets the requested architecture | Main tradeoff | Recommendation |
| --- | --- | --- | --- |
| RHCOS, Ignition, local workload archives, host Podman | Yes, subject to the runtime proof | Requires disk packaging and a standalone RHCOS lifecycle decision | Preferred candidate. |
| RHCOS, Ignition, first-boot image pulls, host Podman | Yes after successful pulls | Requires registry access at first boot | Useful first experiment, not the self-contained deliverable. |
| RHCOS with host FRR RPM | Requires an additional package or OS composition mechanism | Exact RPM, dependencies, base compatibility, and updates become another workstream | Defer because the image alternative meets the request. |
| Boot the current bootc appliance as the OS | Avoids nested Podman, but retains bootc and the Containerfile | Existing packaging model | Comparison baseline, not this proposal. |
| Run the whole current appliance image inside Podman on RHCOS | Creates the outer appliance-container layer the user wants to avoid | Inner services, nested workload runtime, and host visibility need extra configuration | Do not select. |

## Implementation phases and completion evidence

The phases below define completion evidence. Local prototype files exist, but
none of these runtime or product qualification phases is complete.
Engineering and release engineering own the implementation roles below only
after assignment in the decision record.

| Phase | Deliverable | Completion evidence |
| --- | --- | --- |
| 1. Resolve and inspect the inputs | One inventory for RHCOS, tools, both images, architecture, checksums, and runtime flags | Authenticated image inspection confirms both digests. FRR version and packaged metrics behavior are recorded. The exact RHCOS guest reports Podman, storage, systemd, cgroup, and SELinux versions or modes. |
| 2. Prove the host runtime | Ignition configuration, payload loader, and two container services | A disposable RHCOS guest runs FRR, node-exporter, and metrics in exactly two containers without Kubernetes or an outer appliance container. FRR establishes a test session, and both exporters return the expected data. |
| 3. Prove the self-contained disk | Builder for a pristine installed disk with local archives and destination Ignition | With registry egress blocked from its first boot, a fresh disk loads both images and passes the runtime checks. Two fresh clones get distinct host identities. A second boot recovers every service. |
| 4. Qualify failure and recovery | Repeatable acceptance checks tied to the artifact checksum | Corrupt archives fail readiness. An unavailable FRR socket fails metrics readiness. Killing each workload exercises restart recovery. A changed test peer changes an FRR metric. No successful test depends on an image already cached by an earlier run. |
| 5. Resolve product delivery and lifecycle | Owned support, update, rollback, and artifact contracts | Named owners approve standalone RHCOS and OCP payload use, reconcile Jira wording, and demonstrate updates and recovery. Each shipped platform passes its own boot test. |

The prototype files live under [image/rhcos/](../image/rhcos/README.md).
That directory contains a raw Ignition template, archive loader, FRR supervisor,
and Python builder with offline tests. The builder takes explicit digest-pinned
image references and records them in a generated inventory. It does not inherit
RHEL 10's host-RPM assertions. The README gives external build commands and guest
acceptance checks.

The live checks above replace the earlier plan's sysctl-only success condition.
Rendering Ignition, listing an enabled service, or recording a pull attempt does
not establish that any of the three required payloads works.

## Product decisions that remain open

The three requested payload constraints define this investigation. The broader
[recorded Jira requirements](../plans/context/evpn-aws/pipeline-spec.md) still
apply to product acceptance. In particular, CORENET-7505 describes extracted
metrics, and the delivery plans assume a bootc update source. The proposed
containerized metrics process and RHCOS lifecycle need explicit reconciliation.
No Jira acceptance criterion is silently changed here.

A 4.22 metrics candidate is not a permanent update strategy. If the product
requires a later image whose exporter needs Kubernetes, it needs an approved
standalone exporter change or another approved metrics source. Setting fake
Kubernetes environment variables is not a standalone solution. Bootc would have
the same exporter dependency.

The BGP and BFD exporter does not by itself satisfy CORENET-7504's VNI, MAC,
VTEP, and DF views. That gap needs its own metrics source or scope decision.
Networking owns the proposed resolution.

RHCOS updates normally belong to OpenShift's management lifecycle. This
standalone appliance needs an agreed OS update source, maintenance owner, and
recovery mechanism. OS state, workload image versions, and mutable configuration
need a compatible rollback policy. Ignition is first-boot provisioning, not the
ongoing update controller. PM, architecture, and the platform owners must settle
support and distribution before supported release.

The qcow2 and AMI requirements remain. A new packaging path must replace the
bootc-specific build and publication assumptions. Platform-specific Ignition
delivery, firmware, disk expansion, and cloud boot behavior require separate
tests. An ISO-only experiment does not close either artifact requirement.

Production transports, host utilities needed by Ansible, FIPS, and the existing
EVPN qualification remain separate work. This minimal runtime proof does not
claim parity with every package in the current Containerfile.

## Research outcome

| Research slice | Result | Remaining evidence |
| --- | --- | --- |
| Repository and existing design | The old RHCOS plan omits two required services. The bootc paths themselves do not imply nested Podman. | Replace the incomplete runtime experiment if this proposal is selected. |
| RHCOS provisioning and packaging | Documented host tools and first-boot provisioning support the proposed arrangement. | Prove archive staging and offline boot on a pinned RHCOS artifact. |
| FRR and exporter compatibility | The inspected 4.22 source is a standalone candidate. The inspected 5.1 source requires Kubernetes. | Authenticate, inspect, and run the exact image digests. |

The Redesign from First Principles principle led to choosing the allowed FRR
image directly on RHCOS rather than carrying the existing host-RPM layout into
the new proposal. Prove It Works led to the artifact boot, host-observation, and
real-scrape acceptance checks. This investigation establishes a candidate worth
testing. It does not establish a supported or working appliance release.

Document verification passed the public-safety scan, relative-link checks, and
public source URL checks. The repository's `check-all.sh` passed its safety,
snippet, and link checks. Its 173 historical GitHub pin checks failed because
GitHub CLI authentication was unavailable. The image registry inspections also
failed authentication, as recorded above. These limitations do not represent
failed appliance runtime tests. No such runtime tests were performed.
