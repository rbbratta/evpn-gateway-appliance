# RHCOS ignition plan

The appliance boots a stock RHCOS image. Ignition writes the sysctl file and the WireGuard directory from `image/rhel10/Containerfile`, and a unit pulls the node-exporter image by digest. FRR stays the host RPM on the bootc image. This program does not install it. The stack base is `bootc-image-import` in `openshift/evpn-gateway-appliance`. `ign-butane` is first. `ign-boot` follows it.

## How to read this

One box is one unit of work. Every box names the evidence that checks it. A nested box is a sub-step of the box above it. Check a box only when its evidence exists, a file, a log line, a screenshot, a test run, or a SHA. The body is a how-to. The appendices explain and record.

The program runs `pstack/skills/poteto-mode/playbooks/autopilot-stack.md`. The operator lands the stack. No owner merges. `ign-boot` stops for the operator's review in chat before the root appends it. `ign-butane` is not review-gated. Both stop at stack-ready.

Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

## Program checklist

### Arm the program

- [ ] State the protocol and this plan to the operator, then stop. Start execution only on the operator's explicit go.
- [ ] Read these from trunk at program start. Re-read them at every tick.
  - [ ] `git show origin/main:pstack/skills/poteto-mode/playbooks/autopilot-stack.md`
  - [ ] `git show origin/main:pstack/skills/swarm/SKILL.md`
  - [ ] `git show origin/main:pstack/skills/poteto-mode/playbooks/opening-a-pr.md`
  - [ ] Read the cursor-team-kit `control-cli` skill before any live lane.
  - [ ] `git show origin/main:pstack/skills/principle-prove-it-works/SKILL.md`
  - [ ] `git show origin/main:pstack/skills/principle-sequence-verifiable-units/SKILL.md`
  - [ ] `git show origin/main:pstack/skills/show-me-your-work/SKILL.md`
- [ ] On the operator's go, arm the audit tick as `/loop 1h` with the tick prompt below. Never leave the cadence to memory.
- [ ] Use this tick prompt, verbatim. "Re-read the execution playbook from trunk. Audit the operation against it and fix drift in this tick. Probe every active lane and judge progress by side effects only. Stand down a stuck lane and dispatch its replacement now. Then post a short status message to the operator in chat only when the audit found a tracked change that no earlier status message reported, such as a PR opened, a code-ready head, a round launched or closed, a verdict, a merge, a stuck agent and the action taken, a blocker added or cleared, or a decision only the operator can make. Name every such change and nothing else. Do not repeat a table, the merged list, or an unchanged blocker. If the audit found none, end the turn with no reply text. Either way, log this tick's row in your decision trail. The row names the items reported, or none."
- [ ] On the operator's hold or stand-down, send every owner a zero-writes order at once.

### Spawn owners

- [ ] Spawn one owner per PR with the full lifecycle the execution playbook names.
- [ ] Follow this dependency graph. Start dependent work only after its parent is on the stack, and base the child on the parent branch.
  - [ ] `ign-butane` is first. It branches from `bootc-image-import`.
  - [ ] `ign-boot` after `ign-butane`.
- [ ] Hold the file boundaries. `ign-butane` touches only `image/rhcos/appliance.bu` and `image/rhcos/checkign.go`. `ign-boot` touches only `image/rhcos/boot.go` and `image/rhcos/rhcos-url.txt`.
- [ ] Hold the review gate. `ign-boot` changes the boot the operator watches. It waits for the operator's review in chat with screenshots and a video before the root appends it.

### PR mechanics, for every PR

- [ ] Resolve the forge once. Default to `gh`. If `command -v origin` succeeds and Origin can resolve `openshift/evpn-gateway-appliance`, use `origin pr` for every PR operation. Record any fallback to `gh`. Never require `gt`.
- [ ] Open the PR ready, never draft, per **Opening a PR**. Use the run's built-in PR tool when it has one, else `origin pr create --status open --base <base-branch>` or `gh pr create --base <base-branch>` according to the resolved forge. A stack child targets its parent branch. The root PR targets `bootc-image-import`.
- [ ] Run `butane --strict --pretty --files-dir image/rhcos image/rhcos/appliance.bu`, `go run image/rhcos/checkign.go`, `python3 image/tools/check-image-inventory.py`, and `make verify` once before the PR-facing push. Push with hooks on.
- [ ] Run `/deslop` before each commit and `/no-comments` before review.
- [ ] Triage every Bugbot and security-reviewer comment per `../references/bugbot-triage.md`.
- [ ] Rebase onto current `bootc-image-import` before the code-ready report and babysit. Keep that merge base in fix rounds. Rebase again only at stack prep, on a `git merge-tree` conflict with `bootc-image-import`, or on a CI failure that comes from a change on that branch.

### Verdict and merge, for every PR

- [ ] At the code-ready head SHA and at each later push that changes the patch, run the swarm per `pstack/skills/swarm/SKILL.md`. One gates lane. The ten live lanes from the PR's **Verify, live** block. The perf lane from its **Verify, perf** block. Two or more audit lanes, each with its own focus, that read the diff and the receipts and distrust the PR body. The root audits the receipts in the stack-ready report before the verdict.
- [ ] Clean only when every lane is `PASS`. Findings go back to the owner, including a defect that a lane filed as a note. A new head gets a fresh swarm and a fresh verdict, except for results that stay valid under the patch-id rule in `playbooks/shipping.md`.
- [ ] No owner merges, arms auto-merge, or closes. A clean verdict appends the PR to the one linear stack on `bootc-image-import`. The operator lands the stack bottom-up. A rebase that changes the patch-id voids the verdict and returns through step 4 of `playbooks/autopilot-stack.md`.

### Boot recipe, for every live lane

Each live lane runs on its own cloud VM at the PR head. Drive the terminal through `control-cli` from `cursor-team-kit`.

- [ ] `git fetch origin <head-branch> && git checkout <head SHA>`.
- [ ] For `ign-butane`, run `go run image/rhcos/checkign.go` and wait for `checkign: ignition 3.5.0`. For `ign-boot`, run `go run image/rhcos/boot.go` and wait for the serial line `net.ipv4.ip_forward = 1`.
- [ ] Deliver input only through `control-cli`. Read-only diagnostics are `butane --strict`, `python3 -m json.tool` on the generated Ignition, and the qemu serial log.
- [ ] Save every screenshot to `/tmp/swarm-<pr-id>/worker-<n>/<slug>.png` and return the paths with the report.

## Add the Butane config (ign-butane)

**Depends on.** None.

**Files.**

- [ ] Create `image/rhcos/appliance.bu`.
- [ ] Create `image/rhcos/checkign.go`.

**Build.**

- [ ] `appliance.bu` uses Butane variant `fcos` version `1.6.0` and compiles to Ignition `3.5.0`.
- [ ] Write `/etc/sysctl.d/99-ip-forward.conf` with `net.ipv4.ip_forward = 1` and `net.ipv4.conf.all.forwarding = 1`.
- [ ] Create `/etc/wireguard` mode `0700`.
- [ ] Enable `evpn-image-pull.service` and `node-exporter.service`.
- [ ] `evpn-image-pull.service` runs `podman pull` on the node-exporter digest in `image/rhel10/payload-images.env`.
- [ ] The pull unit sets `ConditionPathExists=/etc/containers/auth.json`.
- [ ] `node-exporter.service` runs that digest with the host proc, sys, and rootfs mounts and `--web.listen-address=127.0.0.1:9100`.
- [ ] Leave `image/rhel10/Containerfile` unchanged. `python3 image/tools/check-image-inventory.py` still passes.
- [ ] `checkign.go` loads the Butane output and fails if any storage path is under `/usr`, if `rpm-ostree`, `frr-container`, or `FRR_IMAGE` appears, or if the file set differs from the list above.

**You see.**

- [ ] `go run image/rhcos/checkign.go` prints `checkign: ignition 3.5.0 files=1 dirs=1 units=2`.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] `image/rhcos/checkign.go` gains the path allow-list and the `/usr` reject. Run `go run image/rhcos/checkign.go`.
- [ ] A fixture that adds `/usr/bin/frr-metrics` makes the same program exit 1. Run `go run image/rhcos/checkign.go -fixture usr`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `grok-4.7-medium` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. Run `go run image/rhcos/checkign.go` at `bootc-image-import` and at the head. Trunk has no `image/rhcos/appliance.bu`. Record that. Gate the Ignition version `3.5.0` and the log line `checkign: ignition 3.5.0 files=1 dirs=1 units=2`. Save `lane-1-regression.png`. Pass when the head prints that line and trunk exits non-zero because the file is absent.
- [ ] Lane 2. Decode the sysctl file from the Ignition JSON. Save `lane-2-sysctl.png`. Pass when the inline text contains `net.ipv4.ip_forward = 1` and `net.ipv4.conf.all.forwarding = 1`.
- [ ] Lane 3. Search `appliance.bu` and the Ignition JSON for `frr-container` and `FRR_IMAGE`. Save `lane-3-no-frr-container.png`. Pass when both searches are empty.
- [ ] Lane 4. Read the directory mode for `/etc/wireguard`. Save `lane-4-wireguard.png`. Pass when the mode is `0700`.
- [ ] Lane 5. Run `go run image/rhcos/checkign.go -fixture usr`. Save `lane-5-usr-reject.png`. Pass when the exit code is 1 and the log names `/usr/bin/frr-metrics`.
- [ ] Lane 6. Search the Butane source and the Ignition JSON for `rpm-ostree` and `dnf`. Save `lane-6-no-rpm.png`. Pass when both searches are empty.
- [ ] Lane 7. Compare the node-exporter pull spec to `image/rhel10/payload-images.env`. Save `lane-7-digest.png`. Pass when the digest `sha256:7d2884dc16f902e31b897d475d3e0dc09bc735df16b22544d0fefc4396a0ca48` is the only node-exporter reference.
- [ ] Lane 8. List `systemd.units` and the `enabled` field. Save `lane-8-units.png`. Pass when `evpn-image-pull.service` and `node-exporter.service` are present, each `enabled` value is true, and no other unit is present.
- [ ] Lane 9. Show the first token of the Butane output. Save `lane-9-json.png`. Pass when it is a JSON object whose `ignition.version` is `3.5.0` and the text does not contain `kind: MachineConfig`.
- [ ] Lane 10. Show the pull unit body. Save `lane-10-auth-condition.png`. Pass when the unit contains `ConditionPathExists=/etc/containers/auth.json` and does not contain a registry password or token.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Wall seconds for `butane --strict` plus `go run image/rhcos/checkign.go`. Trunk has no config, so also time that diff-added command and the end state `checkign: ignition 3.5.0 files=1 dirs=1 units=2`.
- [ ] Probe. Run the command at trunk and at the head, interleaved. Trunk records the missing file. Head records the seconds and the log line.
- [ ] Baseline. Record the trunk result `metric absent` first.
- [ ] Rule. Fail the head when the log line is missing, or when the seconds are greater than 2. Do not compute a ratio against the absent trunk metric. The throwaway compile on this machine took 0.01 seconds.

**Review gate.** None. ign-butane is not review-gated.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Rebased onto current `bootc-image-import` after the verdict, patch-id unchanged.
- [ ] The root appends `ign-butane` onto `bootc-image-import`. The operator lands it.

## Boot RHCOS with that Ignition (ign-boot)

**Depends on.** ign-butane.

**Files.**

- [ ] Create `image/rhcos/boot.go`.
- [ ] Create `image/rhcos/rhcos-url.txt`.

**Build.**

- [ ] `boot.go` reads `rhcos-url.txt`, runs `butane` on `appliance.bu`, embeds the Ignition with `coreos-installer iso ignition embed`, and boots that ISO under `qemu-system-x86_64`.
- [ ] The URL file pins one RHCOS qemu image. The owner records the URL and the SHA256 printed by the download.
- [ ] The guest serial console is the only output the program treats as success.
- [ ] The boot does not write `/etc/containers/auth.json` unless the operator passes a secret path that is not in git.

**You see.**

- [ ] The serial log prints `net.ipv4.ip_forward = 1`.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] `boot.go` exits 2 when `rhcos-url.txt` is empty, before it starts qemu. Run `go run image/rhcos/boot.go -check`.
- [ ] `go run image/rhcos/checkign.go` still prints `checkign: ignition 3.5.0 files=1 dirs=1 units=2`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `grok-4.7-medium` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. Run `go run image/rhcos/boot.go` at `bootc-image-import` and at the head. Trunk has no boot program. Record that. Gate the guest sysctl value and the serial line the operator waits for. Save `lane-1-regression.png`. Pass when the head serial log contains `net.ipv4.ip_forward = 1` and trunk exits non-zero because `boot.go` is absent.
- [ ] Lane 2. Look for `/etc/frr/daemons` in the guest. Save `lane-2-no-daemons.png`. Pass when the path is absent.
- [ ] Lane 3. Read the mode of `/etc/wireguard` from the guest. Save `lane-3-wireguard.png`. Pass when the mode is `0700`.
- [ ] Lane 4. Look for `/usr/bin/frr-metrics` in the guest. Save `lane-4-no-usr.png`. Pass when the path is absent.
- [ ] Lane 5. Run `systemctl is-enabled evpn-image-pull.service` in the guest. Save `lane-5-pull-enabled.png`. Pass when the guest prints `enabled`.
- [ ] Lane 6. Boot with no auth file. Save `lane-6-no-auth.png`. Pass when `evpn-image-pull.service` is inactive and the journal cites the path condition.
- [ ] Lane 7. Boot with a stand-in auth file that is not a registry token. Save `lane-7-pull-attempt.png`. Pass when the journal contains the node-exporter digest `sha256:7d2884dc16f902e31b897d475d3e0dc09bc735df16b22544d0fefc4396a0ca48`.
- [ ] Lane 8. Run `systemctl is-enabled node-exporter.service` in the guest. Save `lane-8-node-exporter.png`. Pass when the guest prints `enabled`.
- [ ] Lane 9. Look for `frr-container.service` in the guest. Save `lane-9-no-frr-unit.png`. Pass when the unit file is absent.
- [ ] Lane 10. Show the Ignition application on the serial console. Save `lane-10-serial.png`. Pass when the console shows Ignition finished and the sysctl line is present.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Seconds from the qemu process start until the serial line `net.ipv4.ip_forward = 1`. Trunk has no guest, so also time that diff-added boot and the end state the operator waits for, which is that serial line.
- [ ] Probe. Run `go run image/rhcos/boot.go` at trunk and at the head, interleaved. Trunk records the missing program. Head records the seconds and the serial line.
- [ ] Baseline. Record the trunk result `metric absent` first.
- [ ] Rule. Fail the head when the serial line is missing, or when the seconds are greater than 180. Do not compute a ratio against the absent trunk metric. 180 is a ceiling. This plan did not boot RHCOS.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane 1 and lane 10 screenshots into `/tmp/ega-review/ign-boot-review-regression.png` and `/tmp/ega-review/ign-boot-review-serial.png`.
- [ ] Record a 30 to 60 second video of the serial console on a lane VM. Save it as `/tmp/ega-review/ign-boot-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at stack-ready. Wait for the operator's review.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Rebased onto the `ign-butane` tip after the verdict, patch-id unchanged.
- [ ] The root appends `ign-boot` onto `ign-butane`. The operator lands the stack bottom-up after the review.

## Close the program

- [ ] Every box above is checked with its evidence.
- [ ] Reply to the operator with the stack root, the stack tip, a one-line verdict per link, and anything parked.

## Appendix A. Prototype evidence

The open question was which Ignition consumer can express the host config. The throwaway is `/tmp/ega-rhcos-ignition-proto` on branch `master` at SHA `e064c7be30af5a29682beafb533d62461ea1fba5`. Butane 2.27.0 compiled `fcos` `1.6.0` to Ignition `3.5.0` in 0.01 seconds. That JSON also had a daemons file and an FRR unit. The plan drops both. The inventory gate forbids them on the product Containerfile, and this program does not add them beside it. No screenshots. The observation is that compile.

`openshift` `4.19.0` rejected `storage.directories`. The same variant from `4.17.0` through `4.20.0` compiled a file and a unit into a `MachineConfig`. That wrapper is the wrong consumer for a stock RHCOS boot.

Butane accepted a file at `/usr/bin/frr-metrics`. `checkign.go` fails that path because ostree does not keep files written under `/usr`. Butane rejected an `rpm-ostree` key under `--strict`.

These stay unproven. No RHCOS guest was booted. No image was pulled from `registry.redhat.io`. The FRR tag in `Containerfile.embed` was not resolved to a digest. The `frr-metrics` binary was not started from the image. The wireguard kernel module was not checked. `/var/run` was not booted. The prototype created that directory, and this plan leaves it out because `/run` is a tmpfs.

## Appendix B. Alternatives rejected

A `MachineConfig` loses directory mode. The appliance is a stock RHCOS boot, so the program uses raw Ignition.

`Containerfile.embed` runs FRR from an image. `image/tools/check-image-inventory.py` fails that shape on `image/rhel10/Containerfile`, and the kickoff table Already settled says RHEL 10 FRR is the host `frr` RPM. Butane variant `fcos` cannot install that RPM. The Ignition file does not add `frr-container.service`.

Copying `/frr-metrics` onto the host matches the product Containerfile. Butane will compile a `/usr` write, and ostree will not keep it. The agent guide also says that binary exits outside Kubernetes on 4.23 and 5.x. Metrics stay out of this program.

Quadlet stays out. `plans/context/evpn-aws/containerfile-refactor-spec.md` says the shipped units are systemd `podman run`.

`evpn-on-cloud/image/Containerfile` already pulls images at boot, including `:latest`. This program does not copy those tags. The node-exporter reference stays the digest in `image/rhel10/payload-images.env`.

The `dnf` packages `wireguard-tools`, `libreswan`, `tcpdump`, `traceroute`, `mtr`, `bind-utils`, and `python3` are not files Ignition can write. They are out of this program. `podman` is already on RHCOS. The pull units call `/usr/bin/podman`.

Baking images with `skopeo copy` into `/usr/lib/containers-image-cache` is the bootc payload path. This program replaces `payload-load.service` with a pull at boot. The Containerfiles stay.

Deleting the bootc tree in the same PRs would mix two changes. A later program can remove it after the guest boot is the one the operator uses.

## Appendix C. Risks

`ign-butane` can drift from `image/rhel10/payload-images.env`. The digest lane watches the node-exporter digest. `image/tools/check-image-inventory.py` does not read `image/rhcos/`. `checkign.go` is the gate for that directory.

`ign-boot` needs a qemu binary and a downloaded RHCOS image. The URL is an owner pin, not a guess in this plan. A missing image fails `boot.go` before qemu.

`registry.redhat.io` pulls need a secret. The secret stays out of git. Lane 6 proves the unit stays idle without it. Lane 7 proves the unit names the digest. A successful pull is not a gate of this program.

Already settled, dated 2026-10-07, says RHEL 10 FRR is the host `frr` RPM and `frr.service`. This program leaves that row in place. Say if the Ignition file should pull the FRR image anyway. Gate C-stage still requires a qcow2 and an AMI. This stack does not publish either one. WireGuard remains development and test only, so `/etc/wireguard` is the directory from the Containerfile and not a production transport.

`/usr/bin/podman` on the chosen RHCOS build was not checked here. Lane 5 fails if the binary is missing.

The live lanes have a terminal and a serial console. `control-cli` drives the terminal. There is no browser control skill for the guest console. The serial log is the guest evidence.

## Appendix D. Links and reading list

Read `image/rhel10/Containerfile`, `image/rhel10/payload-images.env`, `image/rhel10/node-exporter.service`, `image/tools/check-image-inventory.py`, and `plans/context/evpn-aws/kickoff-decisions.md` under Already settled. Read them from `bootc-image-import` at `67524c7`. The worktree copy of `Containerfile.embed` has a broken COPY line. HEAD does not.

The embed command to copy is `coreos-installer iso ignition embed` from `ci-operator/step-registry/baremetal/lab/agent/install/ephemeral/gen-unconfigured-image/`. Step-registry Butane examples use older specs, including Ignition 3.0.0 and `fcos` 1.1.0. This program keeps the 1.6.0 compile from the prototype. `ignition-validate` is not in the appliance tree.

`ign-boot` gets `pstack/skills/how/SKILL.md` before the qemu flags are chosen. `ign-butane` does not. Neither PR gets `pstack/skills/interrogate/SKILL.md`. The consumer fork was settled by the Butane compile. The FRR fork was settled by the inventory gate.

The decision trail follows `pstack/skills/show-me-your-work/SKILL.md`. Each owner keeps `decisions.tsv` uncommitted and returns it in the report.
