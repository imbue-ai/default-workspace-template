# Make earlyoom honor the priority bands under gVisor

## Overview

- **The bug.** gVisor serves `/proc/<pid>/oom_score` as a constant `0` (google/gvisor `pkg/sentry/fsimpl/proc/task.go:92`). It stores `oom_score_adj` but never uses it (gvisor#1967, open since 2020).
  - earlyoom picks the highest `oom_score`. With every score 0, it falls back to largest VmRSS.
  - So every gVisor workspace (all imbue_cloud, aws, vultr) sheds its biggest process first, and the `oom_priority` bands do nothing.
  - Evidence on geebspace: `chat-app` (band ~25) was SIGTERMed before `pytest` (band 900). Four chats and a worker were shed 37 minutes before any Chromium process.
- **The fix is a fork: `imbue-ai/earlyoom`.** A public GitHub fork of `rfjakob/earlyoom` at v1.9.0, released as `v1.9.0-imbue.N`.
  - The victim score is computed from `oom_score_adj` the way the kernel does, instead of read from `/proc/<pid>/oom_score`.
  - It applies unconditionally, with no gVisor detection. On runc it reproduces the kernel's own ordering.
  - Upstream sync is not a goal.
- **The score is the kernel's badness.** `badness = VmRSS + VmSwap + VmPTE + oom_score_adj * (MemTotal + SwapTotal) / 1000`, validated against `mm/oom_kill.c:228-234` and `fs/proc/base.c:583-602`.
  - Under gVisor, SwapTotal is 0 and VmSwap/VmPTE are absent, so this reduces to `VmRSS + adj * MemTotal / 1000`.
  - The bands stay a soft steer, identical on gVisor and runc: one band point is worth MemTotal/1000 of RSS.
- **The fork is a drop-in replacement.** It accepts the same flags and keeps the same `-N` contract as the binary existing workspaces run, so it can be swapped into live workspaces without touching their tree.
- **Workspaces install it during setup, not vendored.** The fork's CI publishes per-arch static binaries plus `.sha256`, like `imbue-ai/owner-exec`.
  - A pinned dwt installer, `install_earlyoom.sh`, is chained from `setup_system.sh`. The apt package is dropped.
  - update-self re-runs `setup_system.sh` whenever a chained installer changes, so existing workspaces pick it up through a normal release.
- **Existing imbue_cloud workspaces get it early.** A new `minds-admin hotpatch-earlyoom` command (gen-2 slices only) swaps the binary in and restarts earlyoom, ahead of the dwt release.
- **The docs that state the wrong model are corrected.** That covers bands.py, the oom_priority README, OOM_DRILL.md, `earlyoom.conf`, the oom-graceful-degradation plan, AGENTS.md, and the monorepo's `bare_metal.py` docstring.

## Expected behavior

### Victim selection

- Under gVisor, earlyoom sheds in band order, adjusted by memory:
  - Chromium and agent subprocesses go first.
  - Then stale chats, workers, engaged chats, user services and built-in services.
  - Never-kill infrastructure is shielded by `--avoid`.
- A process's effective score is `RSS + adj * MemTotal / 1000`. A large memory gap can still reorder adjacent bands; that is the same soft-steer behavior runc already has.
- On runc (lima, macOS/Linux local Docker) the order is unchanged. The fork reproduces what the kernel's `oom_score` already gave.
- `--avoid` and `--prefer` stay worth ±300 of adj, i.e. ±300 × (MemTotal+SwapTotal)/1000 in badness units. That is the weight they had against `oom_score`.
- A process at `oom_score_adj -1000` is never picked (upstream's skip, kept).
  - Under gVisor any process can set -1000, because gVisor doesn't enforce the `CAP_SYS_RESOURCE` floor.
- Kernel threads are still skipped, but no longer by pid number.
  - Inside a container pid namespace, pid 2 and its children are ordinary processes. Upstream's `pid <= 2` and `ppid == 2` checks wrongly exempt them.
- earlyoom never picks itself.
- Thresholds (`-m 10,5 -s 10,5`), SIGTERM-wait-SIGKILL escalation, and `-r 0` behave exactly as before.

### The kill hook and ledger

- The `-N` hook still runs with no arguments and gets `EARLYOOM_PID/UID/NAME/CMDLINE` as before.
- It also gets these new variables:
  - `EARLYOOM_OOM_SCORE_ADJ`, `EARLYOOM_BADNESS_KIB` and `EARLYOOM_VMRSS_KIB`.
  - `EARLYOOM_ORDERING`: `kernel_badness`, or `upstream_fallback` if the self-check failed. `EARLYOOM_BADNESS_KIB` is set only under `kernel_badness`.
- On the released tree, each `process_shed` ledger line records `oom_score_adj`, `badness_kib`, `vm_rss_kib` and `ordering`. Fields the hook didn't receive are `null`.
- A hotpatched workspace, which still has the old hook, records kills exactly as today. Its earlyoom log still shows each victim's adj.
- The kill log line gains `oom_score_adj N` (from upstream v1.9.0). Nothing parses that line.

### Startup self-check

- At startup, earlyoom reads `/proc/self/oom_score_adj`, `/proc/self/status` VmRSS and `/proc/meminfo` MemTotal.
- If any can't be read or parsed, it keeps running and chooses victims exactly as upstream v1.9.0 does. It logs an ERROR at startup and on every kill, and marks each kill `upstream_fallback`.
- It never exits over this. A restart loop under supervisord would leave the workspace with no early shedding at all.

### Install and upgrade

- A new image, or a live re-provision, installs the pinned binary at `/usr/local/bin/earlyoom` and writes the stamp `/usr/local/bin/.earlyoom-version`.
- The conf runs `/usr/local/bin/earlyoom` by absolute path.
- The apt `earlyoom` package is no longer installed.
  - On an existing host, the re-provision purges it after installing the fork.
  - On systemd hosts (lima), the packaged `earlyoom.service` is disabled and masked. Today it runs a second, unconfigured daemon: no `--avoid`, no hook.
- An update-self rollback re-runs the old `setup_system.sh`, which reinstalls apt earlyoom and restores the bare `earlyoom` in the conf.
  - The purge never touches `/usr/local/bin`, and `/usr/local/bin` comes first in PATH (the pinned-restic install already relies on this), so the host keeps running the fork.
- The monorepo's apt mirror list keeps `earlyoom`, so the rollback's `apt-get install` still works.

### The hotpatch (existing imbue_cloud gen-2 workspaces)

- `minds-admin hotpatch-earlyoom` probes by default. `--all-leased` only ever probes; `--apply` takes named `--host-id`s plus the tier confirmation flags.
- `--apply` on one host:
  - Takes a per-VM lock and records earlyoom's command line.
  - Inside the container: downloads the pinned x86_64 asset, checks its sha256, installs it at `/usr/local/bin/earlyoom`, and writes the stamp that `install_earlyoom.sh` writes. The later installer is then a no-op.
  - Runs `supervisorctl restart earlyoom`. No chat or service is interrupted; shedding pauses for about a second.
  - Then verifies:
    - earlyoom is `RUNNING`;
    - `/proc/<pid>/exe` is `/usr/local/bin/earlyoom`;
    - its command line is identical to before;
    - its log shows the fork's version line and no self-check ERROR.
  - If any check fails, it puts back what was there before (an earlier fork release and its stamp, or nothing, leaving `/usr/bin/earlyoom`), restarts earlyoom again, and reports `failed`.
- A host reports `blocked`, and is left alone, if any of:
  - its earlyoom process's PATH doesn't put `/usr/local/bin` ahead of `/usr/bin`, or supervisord runs it by an absolute path other than `/usr/local/bin/earlyoom`;
  - its dry run is flagged (below), unless `--force` is passed;
  - earlyoom is not `RUNNING` under supervisord, or the container is not x86_64.
- Any stamp other than the pinned version counts as needing the swap, so re-running `--apply` upgrades hosts swapped earlier.
- The probe reports, per host:
  - the earlyoom process's exe, command line and PATH, and the stamp;
  - pid 1 and pid 2;
  - a dry run of the fork's ordering, computed from each process's adj and RSS.
- A dry run is **flagged** when a process at adj ≤ 80 (a built-in service, the primary agent, infrastructure) would rank above any process at adj ≥ 900 (agent subprocesses, Chromium).
  - That points at a tagging bug that the working bands would turn into a real victim.

## Implementation plan

### `imbue-ai/earlyoom` (new public fork, branched from upstream tag `v1.9.0`)

- `kill.c`
  - `is_larger()`: replace the `oom_score` comparison with a `badness` comparison, where `badness = VmRSS + VmSwap + VmPTE + adj * (MemTotal + SwapTotal) / 1000` (all in KiB).
    - Read `oom_score_adj` before comparing, not only after winning as upstream does.
    - Keep the `-1000` skip, `--ignore`, and uid filtering.
    - Apply `--prefer`/`--avoid` as `±300 * (MemTotal + SwapTotal) / 1000` KiB.
    - Keep the upstream tie-break on RSS.
    - Pass `meminfo_t` into `is_larger()`/`find_largest_process()`, as upstream master does.
  - Replace `pid <= 2` / `ppid == 2` with "no mm": skip a process whose `/proc/<pid>/status` has no `VmRSS` line. That mirrors `oom_badness()`'s `!p->mm` skip, and it holds inside a pid namespace.
  - Keep skipping pid 1: the container's init, `is_global_init` in the kernel.
  - Add a `getpid()` self-skip.
  - `notify_spawn_subprocess()`: also `setenv` `EARLYOOM_OOM_SCORE_ADJ`, `EARLYOOM_BADNESS_KIB`, `EARLYOOM_VMRSS_KIB` and `EARLYOOM_ORDERING`.
  - The kill log line adds `badness` and `ordering`.
- `kill.h`: `procinfo_t` gains `badness_kib` (and `VmSwapkiB`/`VmPTEkiB`); `poll_loop_args_t` gains the ordering mode.
- `proc_pid.c` / `proc_pid.h`: a parser for `/proc/<pid>/status` that reads `VmRSS`, `VmSwap` and `VmPTE`, treating a missing field as 0, and reports "no VmRSS" separately.
- `meminfo.c`: ensure `SwapTotal` is parsed (it already is for `-s`).
- `main.c`: the startup self-check (see Expected behavior) sets the ordering mode, and ERRORs name the unreadable input.
- Test suite (`testsuite_helpers.go`, `testsuite_unit_test.go`, `testsuite_cli_test.go`):
  - `mockProc` writes a real `oom_score_adj` and a `status` with `VmRSS`/`VmSwap`/`VmPTE`, and can omit `VmRSS` (a kernel thread).
  - New ordering cases:
    - adj beats RSS within the MemTotal/1000 exchange rate;
    - every `oom_score` is 0 (gVisor) and the order still follows adj;
    - swap terms count;
    - `--avoid`/`--prefer` scaling;
    - -1000 is skipped;
    - pid 2 and its children are eligible, and a no-mm process is skipped;
    - earlyoom doesn't pick itself;
    - an unreadable adj produces `upstream_fallback` ordering.
- `.github/workflows/ci.yml`: keep upstream's build and `make test`.
  - Add an x86_64 job that installs runsc at the production pin (`PINNED_GVISOR_RELEASE` in the monorepo's `libs/mngr_vps/imbue/mngr_vps/host_setup.py`, currently `20260601`).
  - That job runs the built binary under `docker --runtime=runsc` with a memory limit and processes tagged to different bands, then asserts that the kill order follows badness.
  - It runs on push only.
- `.github/workflows/release.yml` (new, modeled on owner-exec's):
  - On a `v*` tag, build static binaries (`CFLAGS=-static` via the environment, as upstream's Dockerfile does) for x86_64 and aarch64 (native `ubuntu-24.04-arm` runner).
  - Publish `earlyoom-x86_64-unknown-linux`, `earlyoom-aarch64-unknown-linux` and their `.sha256` files.
  - `VERSION` comes from `git describe`, so the startup line reads `earlyoom v1.9.0-imbue.N`.
- `README.md`: a short fork section covering why it exists, the scoring change, the new `-N` variables, and the ordering modes.

### default-workspace-template

- `system/scripts/install_earlyoom.sh` (new): modeled on `install_owner_exec.sh`, with per-arch sha256s pinned in the script like `install_dufs.sh`.
  - It pins `EARLYOOM_VERSION`, is idempotent and gated on the version stamp, and installs atomically (`install` to `.new`, then `mv`).
  - The header comment explains how to bump it, and says the `minds-admin hotpatch-earlyoom` pin must move with it.
- `system/scripts/setup_system.sh`:
  - Drop `earlyoom` from the apt list.
  - Chain `install_earlyoom.sh` next to the owner-exec and dufs installers, with the same `default-workspace-template-install-earlyoom` fallback.
  - Afterwards, `apt-get purge -y earlyoom` if it's installed.
  - In the existing `systemctl` block, disable and mask `earlyoom.service`.
  - Update the comment above the apt list.
- `system/Dockerfile`: `COPY` the installer to `/usr/local/bin/default-workspace-template-install-earlyoom` and add it to the `chmod` line.
- `system/supervisord.conf.d/earlyoom.conf`: `command=/usr/local/bin/earlyoom ...` with the flags unchanged, and the header rewritten to describe the badness model.
- `system/services/oom_priority/bin/earlyoom_record_shed.py`: read the new `EARLYOOM_*` variables (missing → `None`) and pass them through.
- `system/services/oom_priority/src/oom_priority/ledger.py`: `append_shed_record` gains optional `oom_score_adj`, `badness_kib`, `vm_rss_kib` and `ordering`.
- `system/services/oom_priority/bin/oom_drill.py` (new, stdlib-only, runnable as `python3 -` over `mngr exec`):
  - Starts sleepers, each about 3% of MemTotal, tagged to a given band list.
  - Starts a hog at `oom_score_adj -1000` that grows in small steps and pauses after each kill.
  - Snapshots every process's adj and RSS each second, and reads kills from the ledger and the earlyoom log.
  - Aborts, killing the hog, on a wrong victim: one that wasn't the top predicted badness among eligible processes at that moment.
  - Stops the hog after the last sleeper goes.
  - Prints a JSON verdict, plus `/proc/meminfo` (AnonPages, Shmem, Cached, MemFree) at each kill.
- `system/services/oom_priority/src/oom_priority/bands.py`: correct the module docstring and the `SERVICE_BANDS` comment to describe the fork's badness.
  - Also correct the adj-lowering claim: runc enforces the `CAP_SYS_RESOURCE` floor, and gVisor doesn't. Only after measuring it (see Open questions).
- `system/services/oom_priority/README.md`, `OOM_DRILL.md` (point at `oom_drill.py`), and `docs/system/blueprint/oom-graceful-degradation/plan-oom-graceful-degradation.md`: correct the `oom_score` model and the "only ordered mechanism under gVisor" claim.
- `AGENTS.md`: replace the provider list with "some providers run the container under gVisor". Check with `dmesg 2>/dev/null | grep -q 'Starting gVisor'` (`uname -r` also reads `4.19.0-gvisor`).
- Tests:
  - Static test: `earlyoom.conf` execs `/usr/local/bin/earlyoom`, `setup_system.sh` chains `install_earlyoom.sh`, and `earlyoom` is absent from the apt list. It lives in the existing layout or provisioner test file.
  - `ledger` and `earlyoom_record_shed` tests: new fields recorded; absent variables → `null`.
  - `oom_drill.py`: tests of its pure pieces (the badness prediction, the wrong-victim check).
- Changelog entries: `system/changelog/` and `system/services/oom_priority/changelog/`, named after the branch.

### mngr-internal, PR 1: the hotpatch command (stacked on #1316, `gabriel/repair-tmp-exec`)

- `apps/minds_admin/imbue/minds_admin/cli/earlyoom_admin.py` (new): `minds-admin hotpatch-earlyoom`, following `tmp_exec_admin.py`.
  - Options: `--host-id`, `--all-leased` (probe only), `--apply`, `--force` (past a flagged dry run), and the tier confirmation flags.
  - Targets gen-2 rows only, and dials the VM through `vm_root_outer`.
  - `--apply` over several hosts stops at the first `failed`, keeps going past `blocked` and `unreachable`, and emits a JSON report.
- `apps/minds_admin/imbue/minds_admin/slices/earlyoom_hotpatch.py` (new):
  - The pinned fork version and x86_64 sha256, with a `# CLEANUP:` comment: delete this once a survey finds no host still on the apt binary.
  - The in-VM script builder, using `docker exec` into the workspace container. It has probe and apply modes, and apply includes the rollback.
  - Result parsing via `marked_script_output.py`.
  - The pure dry-run ranking and flag check over the probe's raw `(pid, comm, adj, rss_kib)` rows.
  - Outcome and report models.
- `apps/minds_admin/imbue/minds_admin/slices/earlyoom_hotpatch_test.py` (new): fake-VM tests covering verdict parsing, the flagged-host block, the PATH-order block, each rollback branch, and the stamp-mismatch upgrade.
- `apps/minds_admin/imbue/minds_admin/cli/root.py`: register the command. `apps/minds_admin/README.md`: one line.
- `apps/minds/docs/deploy/history/rollouts/earlyoom-gvisor-shed-order.md` (new): the symptom, the root cause, who is affected and who the command can't reach, what the command does, and survey counts per class and tier.
- Changelog entries: `apps/minds_admin/changelog/` and `apps/minds/changelog/`.

### mngr-internal, PR 2: guards and doc fix (off `main`, lands with the dwt PR)

- `apps/minds/test_aws_workspace_oom_release.py` (new, `release` only): a sibling of `test_aws_workspace_release.py`.
  - Creates a full dwt workspace (`--template main --template aws`) under runsc on EC2, and asserts gVisor with the `dmesg` probe.
  - Stops the browser and every chat, then streams `oom_drill.py` in with sleepers from band 1000 down to the service bands.
  - Asserts both strict descending band order of the sleeper sheds and the drill's prediction check, reading the ledger's new fields.
- `apps/minds/docs/deploy/ops/app-release.md`: a step that runs the test whenever a release changes `install_earlyoom.sh`, the runsc pin, or `oom_priority`. `apps/minds/docs/testing-overview.md`: a row for it.
- `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/slices/bare_metal.py`: correct the docstring claiming the host cgroup OOM killer is steered by the bands. It isn't under runsc.
- Changelog entries: `apps/minds/changelog/` and `libs/mngr_imbue_cloud/changelog/`.

## Implementation phases

1. **Fork and first release.**
   - Create `imbue-ai/earlyoom` from upstream `v1.9.0`, then add the scoring change, the kernel-thread/self skips, the self-check, the hook variables, the tests, the runsc CI job and the release workflow.
   - Tag `v1.9.0-imbue.1`.
   - Result: published binaries that pass mock-`/proc` and runsc ordering tests.
2. **The hotpatch command (mngr PR 1).**
   - Build it against the published release.
   - Result: probe and apply work against a fake VM in tests.
3. **The dwt change.**
   - The installer, `setup_system.sh`/Dockerfile/conf, the hook and ledger fields, `oom_drill.py`, the doc corrections and the tests.
   - Result: a branch whose image and live re-provision install the fork.
4. **Staging rehearsal** on a freshly baked gen-2 staging workspace:
   - probe (reports the apt binary);
   - control drill on stock earlyoom (must fail);
   - `--apply`, then drill 1;
   - the branch's `setup_system.sh` on top, then drill 2;
   - destroy the workspace.
   - Plus the lima runc drill.
5. **Sweep.**
   - Survey, then apply staging, then production. Production waits for the owner's go-ahead.
   - geebspace gets the hotpatch, then a probe whose `--dryrun` pick of the installed fork is the predicted first victim. Nothing in the sweep stops a user's chats or browser; the drill is for disposable workspaces only.
   - Any tagging bugs the survey finds get fixed in the dwt PR, or a follow-up if found after merge.
6. **Guards and release (mngr PR 2).**
   - The AWS release test: a control run against dwt `main` fails, then the branch passes.
   - The runbook step and the `bare_metal.py` fix.
   - Merge the dwt PR; it ships in the next minds release.

## Testing strategy

- **Fork unit tests (mock `/proc`).** The ordering cases listed above. These are the tests that would have caught the bug: all-zero `oom_score` with differing adj.
- **Fork runsc CI job.** The real binary, the real gVisor `/proc`, and a real memory limit. The kill order follows badness, with the band's adj beating a larger RSS.
- **dwt unit and static tests.**
  - The conf and installer wiring.
  - The ledger and hook fields, including missing variables.
  - The drill's prediction and wrong-victim logic.
- **Command unit tests (fake VM).**
  - Every verdict: probe, applied, blocked (dry-run flag, PATH order), failed with rollback, unreachable.
  - Stamp-mismatch upgrade; stopping at the first `failed`.
- **Staging rehearsal (acceptance, real gVisor):**
  - The before-probe reports `/usr/bin/earlyoom`. The control drill on stock earlyoom fails, shedding sleepers out of band order.
  - After `--apply`: `exe` is `/usr/local/bin/earlyoom`, the stamp matches, and drill 1 passes with the old hook in place.
  - After the branch's `setup_system.sh`:
    - the installer downloads nothing (the stamp is unchanged);
    - `dpkg -s earlyoom` reports the package absent;
    - the conf runs the absolute path;
    - drill 2 passes, with `oom_score_adj`/`badness_kib`/`ordering` populated in the ledger.
- **Lima drill (acceptance, runc, aarch64, the owner's `minds-host-9dcc81e54493424591859d55199aed74`).**
  - The branch's `setup_system.sh` masks and stops `earlyoom.service` and purges the package, leaving one earlyoom process.
  - The drill passes, and its order matches the kernel's `oom_score` order.
- **geebspace.** Hotpatch, then a probe. It passes if the installed fork's `--dryrun` pick (read-only: it logs the victim and sends nothing) is the predicted first victim.
- **AWS release test.** A control run against dwt `main` must fail on ordering; the branch must pass.
- **Pass criterion for realistic drills.** Each victim had the highest predicted badness among eligible processes when it was shed, and no built-in service was shed while any process at adj ≥ 900 remained.

## Open questions

- **`MemAvailable == MemFree` under gVisor.** The drills record `/proc/meminfo` at each kill. Settle from that data, plus the runsc eviction mode (`pgalloc` `DelayedEviction` / `UseHostMemcgPressure`), whether page cache makes earlyoom fire early. Out of scope for this fix.
- **Modal workspaces (CI only) report MemTotal = 1 TiB.** mngr_modal passes only a memory request, so earlyoom never fires there. A follow-up could set a hard limit equal to the request.
- **Does gVisor skip the `CAP_SYS_RESOURCE` check when lowering adj?** The source says yes (no check in `task_files.go`). The Modal probe ran as root, so it didn't prove it. Measure it on the staging workspace, where Docker drops `SYS_RESOURCE`, before rewriting the docs claim.
- **The no-mm kernel-thread check under gVisor.** gVisor has no kernel threads. Confirm that every real process's `status` carries `VmRSS`, so none is wrongly skipped. The runsc CI job and the probe's process dump both show this.
- **Which imbue_cloud lifecycle paths recreate the container?** A recreated container drops the hotpatch until its next update-self. That's acceptable; noted so the rollout guide can say it.
- **aarch64 static build.** Native arm runner versus a cross-compiler. Either is fine; pick whichever the release workflow keeps simplest.
