# Phase-1 checkpoint (2026-08-27): hardened, runsc-native gen-2 slices on the dev canary

Companion to `benchmark-2026-08-27.md` (the numbers) and the handoff `~/handoff/new-fleet-runsc-prototype.md`.
Branch `new-fleet-runsc-prototype` (mngr-internal PR #686, base `new-fleet-base`; DWT PR #505).
The program pauses here for Josh's hands-on comparison.

Superseded in part by `benchmark-summary-2026-08-27-tuning.md` (same day): the runsc slice was re-baked
after its data disk turned out to have been reaped at bake time, both VMs now run with 16 vCPUs, and the
runsc slice runs with `--file-access-mounts=exclusive --dcache=200000`.

## The two workspaces

Both baked on the canary box `51.81.208.81` (dev-josh-2, server `03a9a4af-a5c8-47e7-b2d9-5f5dae4e08fe`)
from DWT ref `new-fleet-runsc-prototype` at 8 units / 28 GiB data disk / 20 GiB boot disk, and leased
by the canary test account (`canary-sizing-4b0f3e59@imbue.com`; password under "Test account" in
`blueprint/slice-fleet-variable-sizing/HANDOFF.md`; it has ally-level quotas). Josh's own account
cannot lease a specific row deterministically, so the test account holds them; sign the desktop
(pointed at dev-josh-2) or `mngr imbue_cloud auth signin` in as that account to see both.

| label | host_db_id | mngr host id | VM port | container port | ordinal | container runtime |
|---|---|---|---|---|---|---|
| runc | `5e92065a-cb4f-4afe-98a7-9a95c5a5e8c6` | `host-d1f6916e4b934f7a8ba2ad152e9da2c9` | 22002 | 22003 | 1 | `runc` (kernel `6.12.96+deb13-cloud-amd64`) |
| runsc | `3eaeefcf-1cbf-4b68-bcba-b419d5b042f9` (re-baked; was `67f3ea58-…`, see the tuning summary) | `host-5a146d5fc65d4923a62fff5a4d605762` | 22004 | 22005 | 2 | `runsc` (kernel `4.19.0-gvisor`) |

VM-root SSH for either: `ssh -i <dev pool key> -p <VM port> root@51.81.208.81`; the container is
`docker exec -it $(docker ps -q --filter label=com.imbue.mngr.host-id) bash -l`.

The runc slice was baked with `minds-admin pool create --docker-runtime runc`; the runsc one is the
fleet default. Both carry `--tmpfs /run --tmpfs /tmp`, the 7 GiB container memory cap, and
`--security-opt=no-new-privileges`, so the runtime is the only variable.

## Headline numbers (from `benchmark-2026-08-27.md`; runsc / runc, medians)

netstack, `--overlay2=none`, `/home/user` on the 9p gofer volume, 2 vCPUs, 7 GiB cap, same box:

- Filesystem metadata: `find` over the workspace 64 ms -> 2.24 s (**35x**), `tar` 0.11 -> 0.71 s (6.3x),
  `git status` 42 ms -> 0.28 s (6.6x).
- Syscall loop (`dd` 200k x 4k to /dev/null) 81 ms -> 1.16 s (14x); bulk 512 MB write + fsync 0.64 -> 0.69 s (1.1x).
- Interpreter startup x20: python 0.22 -> 0.69 s (3.2x), node 0.54 -> 1.58 s (2.9x); `compileall` 1.5x;
  `claude --version` 82 -> 110 ms.
- Real workloads: cold `uv sync --all-packages` 2.7 -> 11.4 s (4.2x), `npm ci` 2.6 -> 4.8 s (1.9x),
  `git clone flask` 1.2 -> 2.3 s (1.9x), Fortress launch to first page 1.0 -> 2.5 s (2.4x).
- Container sshd connect from the VM (`127.0.0.1:2222`, 100 connects): p50 0.15 -> 0.36 ms, p95 4.6 -> 24 ms.
- Footprint after the run: container 926 MiB -> 1.05 GiB (`docker stats`), VM used 1.5 -> 1.7 GB.
- earlyoom sheds the python allocator under both runtimes (shed ledger entries recorded); the whole
  supervisord stack (browser, system_interface 200, terminal, owner-exec, share-gateway, ...) runs
  under runsc, vm-exec registered, and the bootstrap created `Chat-1`.

## What was verified live

- Re-prep converged the hardened unit and helper, the `kvm nested=0` modprobe pin, the re-staged
  trixie image with runsc `release-20260601.0` and its `daemon.json`, and the telemetry hash
  manifest (integrity check: 7 checked, 0 drifted).
- Both VMs boot and run under the hardened unit: `systemd-analyze security` reports **1.0 OK** for
  `mngr-slice@1` and `mngr-slice@2`; the guest sees no `vmx` flag; the cidata is `/dev/vdc`
  (374K, RO, iso9660) next to the 20G boot disk and the 28G btrfs data disk mounted at
  `/mnt/mngr-data`; `cloud-init status: done`; the console lands in `journalctl -u mngr-slice@N`
  (~2200 lines per boot + build).
- Runtime properties pinned by the helper at start: `CPUWeight=100 IOWeight=100 TasksMax=1024`,
  `MemoryMax` per the note below, `MemorySwapMax=0`.
- The runsc container: `HostConfig.Runtime=runsc`, `uname -r` = `4.19.0-gvisor`, `/run` and `/tmp`
  are tmpfs (by `statfs`; gVisor's mount table does not list them), `/home/user` is the 9p (gofer)
  volume, `nproc` = 2, MemTotal 7 GiB (the cgroup cap).
- `SLICE_UNIT_OOM_KILLED` fires end to end: two forced journal lines under the `systemd` identifier
  (`mngr-slice@999.service: ... killed by the OOM killer.` / `Failed with result 'oom-kill'.`)
  made the collector emit `MNGR_BOX_SIGNAL SLICE_UNIT_OOM_KILLED {"units": ["mngr-slice@999.service"], ...}`.
- dev-josh-2's connector was redeployed from the branch with `MINDS_WEB_TEMPLATE_REF=new-fleet-runsc-prototype`
  (the boot-disk mirror change), so stop/start of these workspaces runs the branch code.

## Findings to decide on

1. **MemoryMax at the budget footprint swaps guest RAM.** The plan's `MemoryMax=units*1024+512 MiB`
   put both building VMs' cgroups exactly at the cap (`memory.current` = `memory.max`, anon
   ~8.3-8.6 GB, file ~0.5-0.8 GB, `pgscan` 1.6M pages) with **~290-330 MB of guest RAM pushed to the
   box swapfile** (`memory.swap.current`), while the box itself had 89 GB free -- the cap, not box
   pressure, caused the swapping (the prototype's "288.3M memory swap peak" was the same effect).
   An uncapped VM (the old unit, ordinal 0) meanwhile holds **15.6 GB of host page cache** for its
   disk images, so some cap is wanted. The branch now sets the cap 512 MiB above the footprint
   (`GEN2_UNIT_MEMORY_MAX_HEADROOM_MIB`) and `MemorySwapMax=0`, so the cap clamps only host page
   cache and an overrun is an OOM kill (the signal) instead of a silently swap-thrashed workspace.
   The two comparison VMs had the new properties applied live after they had already swapped
   (the swapped pages fault back in on access). Decide: keep the 512 MiB headroom (14 VMs could
   overshoot the box budget by 7 GiB in the worst case -- inside the 8 GiB host reserve), or
   revisit `PER_VM_RAM_OVERHEAD_MIB`.
2. **Two concurrent `pool create` invocations for one env are unsupported**: the first bake's
   post-bake orphan reap destroyed the second bake's in-flight VM (its docstring warns about
   exactly this). The bake retried with a fresh slice and succeeded; bake serially or with
   `--count 2`.
3. **DWT main is currently incompatible with mngr main** for bakes: every harness plugin renamed
   `auto_dismiss_dialogs` -> `auto_dismiss_dialogs_at_startup` with no alias, and DWT main's
   `.mngr/settings.toml` still uses the old name for claude/codex/pi/antigravity, so
   `mngr create` fails with `Unknown fields in agent_types.claude`. Fixed on the DWT branch
   (overlaps DWT PR #458).
4. **The `just bake-slice-dev` recipe reads `DEFAULT_WORKSPACE_TEMPLATE_DIR` from `apps/minds/.env`**
   (Josh's personal DWT checkout), not the `.external_worktrees` worktree; pass the worktree path
   explicitly when baking a branch.
5. `ReadWritePaths` needed the `-` prefix (see the unit's comment): systemd sets the bind mounts up
   for the `+` root helper steps too, and `run/` does not exist at a slice's first start. Caught
   by the first bake; the prototype had re-used an existing slice dir.

## Not done / deferred

- The first benchmark pass recorded `claude_prompt_round_trip` and `earlyoom_pressure` as failed
  for script reasons (`claude -p` read the rest of the script from stdin; the allocator's expected
  SIGKILL counted as a failure); both were fixed and re-run, see the report's rows.
- The old available row `dd33e3e3-...` (ordinal 0, baked from `mngr/new-fleet-testing`, 32 GiB
  boot disk, old unit) was left in place; it is not part of the comparison.
- No SSH-latency measurement from the box through the DNAT path (the report measures VM
  `127.0.0.1:2222`, i.e. the netstack cost in isolation).
