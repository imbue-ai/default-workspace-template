# Slice fleet cutover: from `new-fleet-base` to a gen-2-only fleet on `main`

This plan finishes the slice-fleet generation-2 program. It starts from the state of the
`new-fleet-base` branch (PR #683: the squashed gen-2 stack, deployed only to dev canaries)
and ends with every tier running gen-2 slices, the code landed on `main`, and the gen-1
machinery deleted. It supersedes phases 3-6 of `specs/slice-fleet/spec.md`; phases 1-2
there are done. Mechanics of the gen-2 backend are in `specs/slice-fleet-gen2/spec.md`.

## Overview

- **Gen-2 VMs are born in their final shape.** The hardened `mngr-slice@` unit and qemu
  argv (prototype-verified on the dev canary, `systemd-analyze security` 9.2 -> 1.0), and a
  workspace container created under gVisor (`runsc`, netstack) from the first boot. No
  runtime "flip", no desired-runtime state anywhere, no per-machine override: `runsc` is a
  property of the fleet, like trixie.
- **The gen-1 -> gen-2 transition is a one-time, per-tier cutover** run by operator
  commands (`minds-admin cutover ...`) that are deleted afterwards together with every
  gen-1 code path. Nothing bridges the two generations inside the product: the in-supervisor
  conversion, the mixed-fleet lease filter, the NULL-tolerant sizing fallbacks, and the
  dual-generation dispatch all go.
- **Rollout is incremental, not a flag day** (reworked by phase 5.5, which supersedes the
  original per-tier-window model): workspaces migrate one at a time onto gen-2 target
  boxes (new address and ports; the client re-resolves), gen-1 boxes repave once emptied,
  and the release channels cohort clients onto their generation.
- **The migration rides the product's own stop** (reworked by phase 5.5): each workspace
  moves through the connector's verified three-object stop artifact, copied to a
  cutover-owned rollback prefix so the product's later stops cannot delete it. The
  tooling parks the row where no connector supervisor or watchdog will touch it, then
  transplants the artifact's data disk into a fresh trixie VM with a fresh `runsc`
  container built from the DWT image of the version the workspace has self-updated to.
- **Two-step landing.** Everything pre-cutover merges to `main`; a release is cut from
  `main`, deployed, and the cutovers run from it. The post-cutover deletions are a separate
  later PR. Then the specs fold into one gen-2-only `specs/slice-fleet/spec.md`.
- **One checkpoint.** After phase 1 lands on the dev canary the program pauses: Josh gets a
  runc slice and a runsc slice side by side plus a benchmark report, and tunes before
  anything else proceeds.
- **Deliberately deferred**: the `:22` lockdown on staging/production (their boxes repave
  open; the management plane is brought up later over the existing re-prep path), pmacct
  flows / abuse-response runbook (a stub phase at the end), hostinet (netstack stays),
  Firecracker / Cloud Hypervisor (not pursued). DHCP on the taps was deferred here and
  then landed before the cutover as imbue-ai/mngr-internal#849 (placement-free cidata, no
  cloud-init replay on restore), so a gen-2 restore never rewrites adopted SSH material.

## Expected behavior

User-visible:

- A remote workspace's coordinates do not change across the cutover: same box address,
  same two ports, same host id, same VM and container SSH host keys. The desktop sees a
  stop followed (hours later) by a start.
- During a tier's window a workspace shows `stopped`; a start attempt returns a clear
  "this workspace is being migrated and will come back on its own" error; creating a new
  remote workspace fails with the existing "no capacity available" error. Both are announced
  out of band beforehand (Discord/email), together with the version floor.
- Workspaces must have self-updated their repo to `minds-v0.3.10` or later (the
  `/home/user` layout) before their tier's window; the preflight names the ones that have
  not. The desktop client version is a separate axis and is not part of the floor.
- After migration a workspace runs on trixie, under gVisor. Software that needs ptrace
  tooling, perf/eBPF, FUSE, io_uring, nested container runtimes, or unusual ioctls no
  longer works inside the container; filesystem-metadata-heavy operations are several times
  slower (find/tar/git status 4-8x on the canary); interpreter startup ~1.8x. Documented in
  the user-facing minds docs and the DWT `CLAUDE.md`/`AGENTS.md`, and in the announcement.
- The container's writable layer is rebuilt from the DWT image of the workspace's own
  self-updated version: anything installed outside `/home/user` that env-converge did not
  record is gone (exactly as after today's slow-path rebuild); `/home/user` is intact.
- Machine sizes are unchanged (8 units, the measured data disk); resize continues to work
  and is exercised by the ungated release tests from the CI cutover on.

Operator-visible:

- `minds-admin cutover preflight|drain|repave|restore`, each idempotent, each with
  `--dry-run`, the destructive three behind `--yes-i-mean-<tier>`, each emitting a JSON
  report plus a table (per box / per workspace: outcome, version, sizes, health).
- Preflight refuses the tier's cutover as a whole when any workspace is below the floor or
  not running, or when any box's workspaces would not fit its gen-2 budgets after repave.
  Already-unhealthy workspaces are warnings, not refusals. Backups are not checked.
- Restore reports pass/fail per workspace from a real health probe (sshd banners, services
  agent up, `supervisorctl status` all RUNNING, `system_interface` answering), then bakes
  fresh pool rows (default 2 per box, `--pool-rows-per-box`) at `--pool-tag`.
- A new box telemetry signal, `SLICE_UNIT_OOM_KILLED`, fires if the hardened unit's
  `MemoryMax` backstop ever kills a VM (it must not; pressure resolves inside the container).
- CI's two boxes are reinstalled as gen-2 once, right after the merge to `main`; the
  release suite then runs on gen-2 with `test_machine_resize.py` and
  `test_workspace_stop_start.py` no longer opt-in.

System behavior:

- Before the merge, nothing changes for any deployed tier (gen-1 paths are untouched; the
  runsc/tmpfs knobs ride per-box `-S` overrides the bake applies only to gen-2 boxes, so a
  gen-1 lima bake never sees them).
- After the release deploy and before a tier's window: gen-1 workspaces keep stopping and
  starting on gen-1 boxes; gen-2 artifacts restore only onto gen-2 boxes and gen-1
  artifacts only onto gen-1 boxes (no conversion path exists).
- During a window: rows are parked `stopped` with NULL placement, box link, heartbeat, and
  artifact pointers -- invisible to the retention finalize and the watchdog -- and
  `POST /workspaces/{id}/start` refuses a gen-1 row with no placement (the only way that
  state arises) with a 409, spawning no supervisor.
- After the window: the tier is gen-2 only; the connector's gen-1 code is dead until the
  deletion PR removes it; the archive prefix holds every migrated workspace's data disk
  plus manifest and KEK-wrapped DEK until the dated `next_deploy.md` cleanup deletes it.
- After the deletion PR: `box_generation` remains a constant-2 column; `draining`,
  `server drain` and `server undrain` stay as the box-maintenance primitives (repave, kernel
  reboot, hardware repair, retirement); the adoption flow pins host keys only and disables
  (does not install) any reconciler it meets.

## Implementation plan

### Phase 1 -- hardening + runsc-native gen-2 slices

`libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/slices/qemu_slice.py`

- `render_slice_unit_file`: the prototype-verified sandbox directives (NoNewPrivileges,
  empty CapabilityBoundingSet, RestrictNamespaces, RestrictAddressFamilies=AF_UNIX,
  IPAddressDeny=any, RestrictRealtime/SUIDSGID, LockPersonality, ProtectSystem=strict +
  `ReadWritePaths=` for `by-ordinal/%i/{run,disk.qcow2,datadisk.qcow2}`, ProtectHome,
  PrivateTmp, ProtectKernel*/ProtectControlGroups/ProtectClock/ProtectHostname,
  ProtectProc=invisible + ProcSubset=pid, SystemCallArchitectures=native,
  `SystemCallFilter=@system-service` + `~@privileged @resources`, DevicePolicy=closed +
  DeviceAllow kvm/tun, UMask=0077, MemoryDenyWriteExecute=yes). `Restart=no` stays.
- qemu argv: `-nodefaults -no-user-config`, `-cpu host,vmx=off,svm=off`,
  `-machine q35,accel=kvm,usb=off,smm=off`, `-global ICH9-LPC.disable_s3=1` / `disable_s4=1`,
  cidata as `-drive file=...cidata.iso,format=raw,readonly=on,if=virtio` (drop virtio-scsi +
  scsi-cd), console `-chardev stdio,id=ser0,signal=off -serial chardev:ser0` (drop
  `run/serial.log`; the console lands in journald). Keep virtio-rng, QMP, `-sandbox`.
- Helper `setup`: `systemctl set-property --runtime mngr-slice@$ORDINAL` with
  `MemoryMax=$((UNITS*1024+512))M` (exactly the budget share; the guest boots with
  `UNITS*1024-512` so qemu fits underneath, and the qcow2 drives use `cache=none,aio=native`
  -- decided 2026-08-27 after measuring the footprint-plus-headroom variant), `MemorySwapMax=0`,
  `CPUWeight=$((UNITS*100/8))`, `IOWeight=` same, `TasksMax=1024`, from the strict-shape env
  values. No `MemoryHigh`.
- `_build_firstboot_script`: select the data disk by `blkid -L mngr-data`, falling back to
  the first non-root disk with `RO=0` and no filesystem; never "first non-root disk".
- Constants for the new argv/directives; unit-file and argv inline snapshots in
  `qemu_slice_test.py`; a stub-on-PATH test of the disk selection against a device table
  that includes a read-only iso9660 disk.

`libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/slices/bare_metal.py`

- `SLICE_BOOT_DISK_GIB` 32 -> 20 (image measured ~11 GiB; 14 default machines then fit the
  850 GB box class: 14 x (20 + 28) = 672 GiB < 765 GiB budget). Every consumer follows the
  constant (reserve, restore df guard, units-valid guard, `server list`, docs).

`apps/minds_admin/imbue/minds_admin/slices/bare_metal_prep.py`

- Gen-2 image customize: download the pinned gVisor release (`PINNED_GVISOR_RELEASE` from
  `mngr_vps.host_setup`, same sha512 verification) into the image and write
  `/etc/docker/daemon.json` registering `runsc` with `--overlay2=none` directly (no
  `runsc install` inside the appliance). Re-staging rule unchanged (delete base qcow2 +
  re-prep).
- `options kvm_intel nested=0` / `kvm_amd nested=0` in `/etc/modprobe.d/` (applies at the
  next box reboot; never unloaded live).

`apps/minds_admin/imbue/minds_admin/slices/box_telemetry.py`, `apps/observability/.../box_signals.py`, `alert_provisioning.py`

- New signal `SLICE_UNIT_OOM_KILLED`: the collector's journal scan matches systemd's
  oom-kill result lines for `mngr-slice@*` units; one alert rule like the others.

`apps/minds_admin/imbue/minds_admin/cli/server.py` (`_build_slice_create_args`) and `cli/pool.py`

- For gen-2 boxes the bake adds `-S providers.imbue_cloud_slice.docker_runtime=runsc` and
  `-S providers.imbue_cloud_slice.default_start_args=["--tmpfs","/run","--tmpfs","/tmp"]`.
  Gen-1 bakes get neither. `pool create --docker-runtime` overrides (the runc comparison
  bake for the checkpoint).

`libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/providers/rebuild.py`

- `build_slice_rebuild_provider` forwards `docker_runtime` and `default_start_args` from
  the account config into `SliceVpsDockerProviderConfig` for gen-2 leases
  (`lease_result.box_generation >= 2`); the `is_slice` skip of `apply_host_setup_on_outer`
  in `providers/instance.py` stays (runsc is in the guest image).

Docs and DWT

- DWT: correct the `[providers.docker]` comment claiming host setup applies tmpfs for
  imbue_cloud; add the gVisor note to `CLAUDE.md` and `AGENTS.md`.
- `apps/minds/docs/workspace/`: the "remote workspaces run in a gVisor sandbox" note.
- `specs/slice-fleet-gen2/spec.md` "Hardening that lima blocked": the new items.

Checkpoint deliverables (dev canary)

- Re-prep the canary; bake one runsc slice and one runc slice (`--docker-runtime runc`),
  same size and DWT version, both leased by Josh's account against one dev env.
- A fixed benchmark script (checked in under `apps/minds_admin/scripts/`, deleted with the
  cutover tooling): cold `uv sync --all-packages`, `npm ci`, a git clone, Fortress launch,
  Claude Code startup, terminal/SSH round-trip latency, earlyoom behavior under memory
  pressure; run on both; written report in `blueprint/slice-fleet-cutover/`.
- The program pauses here until Josh has compared them; which gVisor knobs to try is
  decided from the results.

### Phase 2 -- delete the conversion, the mixed-fleet machinery, and the NULL fallbacks

`apps/remote_service_connector/imbue/remote_service_connector/`

- `box_scripts_gen2.py`: delete `render_gen2_conversion_user_data`,
  `render_guest_upgrade_script`, `build_guest_upgrade_*`, `build_guest_reboot_command`,
  `build_guest_release_codename_command`, `GUEST_UPGRADE_*`, the codename constants.
- `stop_start.py`: delete `_run_guest_upgrade`, `_poll_guest_upgrade`,
  `_wait_for_guest_sshd_down`, `_GuestUpgradeError`, `_record_guest_upgrade_failed`,
  `_run_vm_command`, every `is_conversion` branch (inline user-data, the
  `container_ssh_port=0` wait skip, the `guest_upgrade_failed` clearing in the restore
  CAS); `_list_candidate_boxes` filters to the artifact's own generation.
- `hosts.py`: remove the `box_generation` lease request field and its filter.
- Migration `039_sizing_not_null.sql`: backfill `memory_units` (from `attributes.memory_gb`,
  default 8) and `disk_gb`, then `SET NOT NULL` on both. A gen-2 row takes the artifact
  manifest's measured size when present, else the 44 GiB default. A gen-1 row takes the
  size its data disk has AFTER the cutover -- the box formula
  `(disk_gb_box - reserve) / slot_count - 32` (the lima data disk) plus the 16 GiB gen-2
  data-disk base, i.e. `compute_gen1_migrated_data_disk_gib` -- so the disk quota and
  `machines show` count the capacity the machine will have. Delete the lazy backfill in
  `_drive_stop_inner`,
  `_measured_data_disk_gb` and the fallbacks in `_gen2_machine_sizing`, the
  `COALESCE(..., DEFAULT)` in the quota sums and the eviction planner, the
  `machine_disk_backfilled` metric, and the `attributes.memory_gb` mirror updates in the
  three CAS statements (the pool is uniform; leases match the default document).
- The gen-1 bake path stamps `memory_units` and `disk_gb` (the migrated size, via the same
  helper) on insert until phase 6 deletes it.

`libs/mngr_imbue_cloud/`

- Remove `-b generation=` (`ParsedImbueCloudBuildArgs.generation`, `KNOWN_BOX_GENERATIONS`
  validation, the README paragraph). The rebuild provider sizes the container from
  `LeaseResult.memory_units` alone; the field stays Optional under a `CLEANUP` marker (a
  lease without it, from an older connector, logs a warning and rebuilds without a memory
  cap) until every tier's connector serves the sizing columns.

`apps/minds/deployment_tests/`

- Delete `test_machine_migration.py`. Drop the `MINDS_MACHINE_RESIZE_RELEASE_TEST` gate on
  `test_machine_resize.py` so it runs whenever the minds release tier runs. The stop/start
  test keeps its `MINDS_STOP_START_RELEASE_TEST` gate until phase 5: its stop upload
  against the standing gen-1 CI box takes hours, which no CI job budget fits.

`specs/slice-fleet/spec.md`: pointer header noting phases 3-6 are superseded by this plan.

`apps/minds/docs/deploy/host-pool-setup.md`: document that the backup snapshot relies on the
data disk's 4 GiB system reserve (a workspace at quota can still be snapshotted because the
snapshot's metadata and CoW delta land outside the qgroup), which is why `host_backup`
deletes its snapshot as soon as restic has read it.

Follow-ups to land in the phase-2 PR after the main changes above (small, self-contained;
each its own commit):

1. `server prep` re-staged the gen-2 guest image only when the qcow2 was absent on the box;
   it now stamps a content hash of the image URL plus the `virt-customize` script beside
   the image (`<img>.customization-sha256`), and a missing or different marker re-stages
   on the next `prep` instead of silently keeping the old image. Gen-1 (retired by the
   cutover) stays presence-only.
2. `pool destroy`'s default concurrency (8) dropped SSH sessions when the box is reached
   over the onetun dial (each thread spawned its own tunnel with the same operator
   WireGuard identity, and the box's wg0 keeps one session per peer). The dial resolution
   in `box_access.py` is now single-flight -- concurrent threads dialing one box share one
   tunnel -- so the default concurrency stays and `--max-concurrency 1` is no longer needed.
3. Unify the orphan reaper's 2 h age guard (`ORPHAN_SLICE_MIN_AGE_SECONDS`) with
   `ci_slice_sweep.py`'s age logic (`DEFAULT_CI_SLICE_MAX_AGE_HOURS`): one mechanism (the
   slice client's observations and the tier/age classifiers in `bare_metal.py`), two
   named thresholds defined side by side (the reaper's protects an in-flight bake, the
   sweep's 4 h protects an in-flight release run and is also the ci Modal-env sweep's).
   Landing this also made `sweep_ci_slices_on_box` generation-agnostic, which phase 5
   needed for the gen-2 CI boxes.

### Phase 3 -- the shared gen-2 renderer subpackage

Landed 2026-08-28 (branch `new-fleet-phase-3`, stacked on `new-fleet-phase-2`). Decisions
that refined the sketch below: the subpackage uses `FrozenModel` / `@pure` (the connector
now ships `imbue.imbue_common` too, with a transitive import guard so only its
stdlib/pydantic-only modules can be reached) instead of plain tuples; it also holds
`build_qemu_slice_user_data` (the phase-4 restore renders the standard user-data with the
harvested host key), `FIRST_QEMU_BOX_GENERATION`, and the transfer conventions the gen-1
scripts share (transfer dir, status file, object names, restore markers, env-file
renderer), since the restore-reserve / download / resize renderers embed them; it keeps its
own error base (`Gen2ScriptError`) because `imbue.mngr` does not ship. Consumers import the
moved names from `slices.gen2_scripts.<module>` directly (no re-export shims).

`libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/slices/gen2_scripts/` (new; stdlib + `yaml` only)

- Moves from `qemu_slice.py` / `bare_metal.py`: the layout constants (`GEN2_*` paths,
  `GEN2_MAX_SLICE_COUNT`, `FIRST_QEMU_BOX_GENERATION`, markers, placeholders), the ordinal
  derivations (tap/user/unit/MAC//30), `build_qemu_slice_env_file`,
  `build_qemu_slice_network_config`, `build_qemu_slice_meta_data`, the restore meta-data
  renderer, `render_gen2_budget_guard_lines`, `render_gen2_ordinal_derivation_lines`, the
  sizing constants and math (`DEFAULT_MACHINE_UNITS`, `MACHINE_UNITS_STEP`,
  `MAX_MACHINE_UNITS`, `PER_VM_RAM_OVERHEAD_MIB`, `SLICE_BOOT_DISK_GIB`,
  `HOST_RAM_RESERVE_GIB`, `DISK_RESERVE_*`, `compute_box_total_units`,
  `compute_box_disk_budget_gib` (replaced by `compute_gen2_disk_budget_gib` in phase 4),
  `compute_machine_vcpus`, `compute_machine_data_disk_gib`),
  the in-guest oneshot renderers, `build_qemu_destroy_script`,
  `build_qemu_list_instances_command`, and the restore-reserve / download / in-place resize
  script renderers from `box_scripts_gen2.py` (so `minds_admin`'s cutover can use them).
- Its own error base and a plain tuple for the /30 addresses (no `imbue_common`, no
  `pydantic` models, no `@pure`, no `LABEL_HOST_ID` import -- the label string is a
  constant here); the documented exceptions mirror `modal_app_kit`'s.
- `qemu_slice.py`, `bare_metal.py`, `machines.py`, `hosts.py`, `stop_start.py`,
  `box_scripts_gen2.py` import from it; `box_scripts_gen2.py` keeps only the connector's
  transfer-status conventions and the stop/restart/finalize command builders.
- Connector `app.py`: `add_local_python_source(..., "imbue.mngr_imbue_cloud.slices.gen2_scripts", ...)`;
  `_SHIPPED_IMBUE_PACKAGES` gains it; `deploy_constants.py` unchanged (`yaml` is already in
  the pip set).
- New ratchet in `libs/mngr_imbue_cloud/.../test_project_ratchets.py`: modules under
  `gen2_scripts/` import only stdlib and `yaml`.
- Delete `box_scripts_gen2_drift_test.py` and the hand-rendered YAML network-config.
- A local import smoke check that the mounted subpackage resolves as a namespace package
  without its parents' `__init__.py` (a `modal run` against a throwaway function).

### Phase 4 -- the cutover tooling (`minds-admin cutover`)

Detailed design decided 2026-08-28: [`phase-4-cutover-tooling.md`](./phase-4-cutover-tooling.md)
is authoritative for this phase and supersedes the sketch below wherever they differ. The
decisions that changed the sketch: `stopped` rows are started before the window (new admin
start endpoint) rather than refusing the tier; the restore builds a fresh gen-2 data disk and
`btrfs send | receive`s the gen-1 home subvolume into it (no convert/resize/relabel, and the
quota accounts the migrated data); the container is recreated from a harvested `docker inspect`
with a fixed set of overrides; image tars are published per tag to S3 instead of seeding the
boxes' bake tar cache; and the gen-2 disk accounting is fixed as product code (20 GiB root,
swapfile + tar cache on the XFS partition, measured `disk_gb`, a 64 GiB named reserve).

`apps/minds_admin/imbue/minds_admin/cli/cutover.py` (command group), `cli/cutover_drivers.py` (stage drivers) and `slices/cutover_{scripts,types,state,db}.py` (pure renderers, records, state dir, DB helpers), unit-tested; the whole group is deleted in phase 6.

Common

- Env-aware like every other command (pool DSN, pool key, storage config + KEK, admin key
  from the activated env); state dir `~/.minds-<env>/cutover/` (0700) holding
  `workspaces/<host_db_id>.json` (origin box id, address, both ports, host id, host name,
  version, sizes, S3 keys) and `keys/<host_db_id>/` (VM `/etc/ssh/ssh_host_*`, VM
  `/root/.ssh/authorized_keys`, container `/etc/ssh/ssh_host_*`, container
  `/root/.ssh/authorized_keys`), shredded when `restore` completes; `--dry-run` on every
  stage; `--yes-i-mean-<tier>` on drain/repave/restore; JSON + table reports written to the
  state dir.
- Box dials go through the existing `box_access` resolver; VM-root SSH uses the pool key
  pinned to the row's `outer_host_public_key`; container commands via `docker exec` from
  the VM.
- Sizing inputs: before any re-carve, set every gen-2 box row's `cpu_overcommit_ratio` to
  `DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO` (4.0, decided 2026-08-27; existing rows were
  registered at 2.0, and a carve reads the row, not the constant). Existing slice VMs keep
  the vCPU count in their env file until re-carved, which the cutover does anyway.

`cutover preflight` (read-only)

- Per leased gen-1 row: status must be `leased`; VM-root SSH reachable; `git -C
  /home/user/workspace describe --tags --match 'minds-v*' --abbrev=0` inside the
  container (the workspace's version; the nearest tag counts even with commits on top);
  refuse the tier when any version is below `minds-v0.3.10` or the describe fails; run the
  health probe and record warnings; record data-disk virtual size (`qemu-img info -U`).
- Per box: sum `(10 GiB boot + (each workspace's gen-1 data disk + 16 GiB))` -- the
  restore grows the transplanted disk by the gen-2 data-disk base, matching the size
  migration 039 stamped -- against the box's gen-2 disk budget and `8 units` each against
  the unit budget; refuse with the shortfall if a box does not fit itself after repave.
- Report: per workspace `account email` (via the admin accounts API), host name, version,
  floor, health, sizes; per box the fit; the set of distinct versions (the images the
  window will seed).

`cutover drain`

- Unleased `available` rows: `pool destroy` code path (CAS to `removing`, teardown, delete).
- Per leased row, in parallel across boxes, one at a time per box: harvest the four key
  files; `limactl stop <instance>` as `limahost`; upload `~/.lima/_disks/<disk>/datadisk`
  through `zstd | age | s5cmd pipe` (a small gen-1 box script rendered by `slices/cutover_scripts.py`)
  to `s3://<bucket>/<prefix>/cutover/<host_id>/datadisk.zst.age`, encrypted to a fresh age
  identity whose KEK-wrapped form and the manifest (sha256, bytes, virtual size, format,
  version, ports, origin box) go to `.../cutover/<host_id>/manifest.json`; then CAS the row
  `leased -> stopped` with `vps_address`, `ssh_port`, `container_ssh_port`,
  `bare_metal_server_id`, `transition_heartbeat_at`, `artifact_manifest`, `wrapped_dek` all
  NULL. Idempotent: a row already parked is skipped; a partial upload is re-run.
- Finally set each box `draining` (excluded from every candidate list).

`cutover repave`

- Per box (parallel): flip `box_generation` to 2 on the row; `server setup` (OVH reinstall
  as `debian13_64` with the gen-2 partition layout, host key recorded) then the gen-2 prep
  (`server prep`), leaving `[modal_proxy]` unconfigured so `:22` stays open; the box comes
  back `installing` -> not `ready` until `restore` finishes. Idempotent: a box already on
  gen-2 with the prep converged is skipped.

`cutover restore`

- Per box (parallel), per workspace (one at a time per box): reserve on the origin box at
  the row's previous two ports (the gen-2 restore-reserve renderer from `gen2_scripts`,
  given fixed ports instead of the lowest free ones, fresh trixie boot disk, the standard
  gen-2 `user-data` with the harvested VM host key as `ssh_keys` and the harvested VM
  `authorized_keys`); download `datadisk.zst.age` into `datadisk.qcow2` (`qemu-img convert
  -O qcow2` when the gen-1 disk is raw); `qemu-img resize` it to the gen-1 size plus the
  16 GiB gen-2 base (`compute_gen1_migrated_data_disk_gib`, the size 039 stamped on the
  row; the guest's every-boot grow oneshot extends the filesystem and the quota); relabel
  its btrfs `mngr-data` offline (`qemu-nbd` + `btrfs filesystem label`, partition table
  kept); `systemctl enable` + `start`; wait for the VM banner and `cloud-init status
  --wait`.
- Container: ensure the DWT image for the workspace's version is in the guest's dockerd
  (box cache tar per tag; seed lazily via the existing `warm-cache` seed path the first
  time a version is needed on a box); `docker volume create` the bind-options volumes
  (`mngr-host-vol-<hex>` -> `/mngr-btrfs/<hex>`, the snapshot trigger volume);
  `docker create` with the canonical realizer args (name `<prefix><host_name>`, the four
  `com.imbue.mngr.*` labels, `0.0.0.0:2222->22`, the three mounts, `--runtime runsc
  --tmpfs /run --tmpfs /tmp --security-opt=no-new-privileges --workdir=/
  --restart=unless-stopped --memory/--memory-swap` from 8 units, the shared entrypoint);
  `docker cp` the harvested container host key and `authorized_keys`; `docker start`;
  `start_container_sshd`; install the snapshot helper unit (`provision_snapshot_helper_on_outer`)
  and the pool_host template's outer autostart units (the `minds-autostart` installer block).
- Health probe: both banners on the box ports; inside the container `supervisorctl status`
  all RUNNING (after the autostart relaunched the services agent) and `system_interface`
  answering on :8000; a failure marks the workspace failed in the report and leaves it
  `stopped` for a re-run.
- CAS the row `stopped -> leased` with the original address/ports, `bare_metal_server_id`,
  `box_generation=2`, `memory_units=8`, `disk_gb` = the grown size (which 039 already
  stamped; the CAS asserts they agree), `attributes` untouched.
- Box finish: set `ready`; bake `--pool-rows-per-box` (default 2) default rows from
  `--pool-tag` through the bake machinery.
- Idempotent per workspace: a row already `leased` on gen-2 is skipped; a half-restored
  slice dir is reclaimed by the reserve's leftover-dir logic.

Connector guard (small, deleted in phase 6)

- `workspaces.py` `start_workspace`: a row with `box_generation = 1` and NULL placement
  returns 409 `{"code": "workspace_migrating", "message": "this workspace is being migrated
  to new infrastructure and will come back on its own"}` before any supervisor is spawned.

Runbook and comms

- `apps/minds/docs/deploy/gen2-cutover.md`: per-tier prerequisites (release deployed;
  alerting armed: shared token into the tier's Vault leaf + `provision-alerts`; dev-box
  consolidation to one env per box), the announcement text incl. the floor and the gVisor
  note, the command sequence with dry-runs, verification, what to do on a failed workspace,
  and the archive cleanup line for `next_deploy.md` (`CLEANUP:` with a date well after the
  production window).

### Phase 5 -- rollout (incremental; superseded shape)

Reworked by phase 5.5 ([`phase-5.5-incremental-rollout.md`](./phase-5.5-incremental-rollout.md),
authoritative): there is no per-tier flag day. The rollout is incremental:

- Merge the stack (phases 1-4 plus 5.5) to `main`; cut a minds release; deploy each tier
  from it in the normal cadence. From that release on, gen-2 boxes bake only
  minds-v0.6.0+ tags and gen-1 boxes only older ones (the bake-time guard), so the
  release channels cohort each client version onto its generation; a
  `max_box_generation` lease capability field keeps pre-0.6 clients off gen-2 rows even
  on the slow path.
- CI: `server setup` the two CI boxes as gen-2 (infra DB); CI bake/sweep/import already
  dispatch on generation (the CI slice sweep learned gen-2 in the phase-2 follow-ups);
  the release suite runs on gen-2 from then on. Drop the
  `MINDS_STOP_START_RELEASE_TEST` gate on `test_workspace_stop_start.py` here (deferred from
  phase 2: the stop upload is hours against a gen-1 box, minutes against gen-2).
- Beachhead per region: order a fresh production box and repave it gen-2; dev/staging
  repave a box with zero leased rows.
- New workspaces move first: promote a 0.6.x release through alpha -> beta -> stable so
  new creates land on gen-2 in widening cohorts.
- Existing workspaces move via `minds-admin cutover migrate` (per workspace, per user, or
  per source box, onto one named gen-2 target box; parallel invocations use disjoint
  targets), starting with single alpha workspaces and widening; `cutover rollback` puts a
  promptly-failed migration back on gen-1 through the product's own restore.
- Boxes empty as their workspaces migrate off; each emptied box is repaved gen-2 and
  baked with 0.6.x rows, rolling capacity over.
- Endgame: an announced forced-migration sweep of the remaining gen-1 workspaces
  (admin-started as needed), then the last gen-1 boxes repave.

### Phase 6 -- post-cutover deletion (separate PR, after production)

Gate (amended by phase 5.5): zero `box_generation = 1` rows in every tier's DB, in every
status (a stopped gen-1 row's artifact is restorable only by the gen-1 code this phase
deletes), and the rollback horizon declared closed (keep at least one gen-1 box per
region until then). The `max_box_generation` lease field outlives this phase: it is
removed only once the pre-0.6 client population is dead.

- `libs/mngr_imbue_cloud`: `lima_slice.py`, `lima_slice_client.py`, generation dispatch in
  `slice_client.py`, `build_slice_vm_client` collapses to the qemu client; the adoption
  reconciler install/heal (`render_reconciler_*`, `build_reconciler_install_command`,
  `read_reconciler_state`, `_verify_and_heal`'s reconciler branch) replaced by a
  `CLEANUP:`-marked recognizer that disables an installed reconciler on first contact;
  host-key pinning and rotation stay.
- `apps/remote_service_connector`: gen-1 `box_scripts.py` transfer/lifecycle variants,
  every `_is_gen2` branch, gen-1 teardown/reconcile listing, the `workspace_migrating`
  guard. The `draining` handling stays: it is the connector's half of the box-maintenance
  primitive (a draining box is excluded from restore candidates and forces the restore path).
- `apps/minds_admin`: `cutover` group, `key_repair.py` / `repair-keys`,
  `backfill-host-keys`, gen-1 prep/autostart (`build_box_prep_script`), `DEFAULT_LIMA_VERSION`,
  the benchmark script, gen-1 slot display in `server list`/`pricing`. `server drain` and
  `server undrain` stay (box maintenance is generation-agnostic; see
  `blueprint/pre-cutover-fleet-fixes/`).
- `libs/mngr_imbue_cloud/primitives.py`: `box_generation` and `SERVER_STATUS_DRAINING` both stay.
- Docs: `gen2-cutover.md` + the tier reports, `slice-hardening-rollout.md`,
  `slice-restart-wipes-owner-ssh-key.md`, `slice-hostkey-revert-stranding.md`,
  `lima-image.md`, `reboot-resilience-rollout.md` move to `apps/minds/docs/deploy/history/`
  with references dropped; the three specs fold into one gen-2-only
  `specs/slice-fleet/spec.md` (the old two keep pointer headers; `blueprint/` untouched).
- A minds release ships the plugin changes.

### Phase 7 -- flows, enforcement promotion, abuse-response runbook (stub)

- Deliverables: pmacct sampled flows into the pipeline, the `abuse-response.md` runbook,
  per-rule promotion of alert-only rules to enforcement. Exit: flows visible per VM for a
  region; the runbook exists; at least one rule promoted on a clean history. Design (sampling
  rate, NFLOG layout, retention, promotion criteria) is decided at implementation time; see
  `specs/slice-fleet/spec.md` phase 5 and `specs/slice-fleet-gen2/spec.md` for the existing
  discussion.

## Implementation phases

1. **Hardening + runsc-native slices** (one PR on `new-fleet-base`). Exit: canary re-prepped;
   a hardened runsc slice bakes, leases, stops/starts, resizes; the two comparison
   workspaces and the benchmark report delivered. **Program pauses here.**
2. **Deletions + NOT NULL** (one PR). Exit: no conversion code; sizing columns NOT NULL;
   the resize release test ungated; the three follow-ups landed; scoped suites green.
3. **Shared subpackage** (one PR; landed 2026-08-28). Exit: drift test gone; connector container imports the
   subpackage (smoke-checked); scoped suites green.
4. **Cutover tooling** (one or two PRs). Exit: all four commands with dry-run, reports,
   guard, runbook; unit tests; a dev rehearsal completed end to end.
5. **Rollout**: merge to `main`, release, CI reinstall, dev rehearsals, staging + soak,
   production. Exit: every tier gen-2, reports committed.
6. **Deletion PR** + minds release. Exit: no gen-1 code; specs folded; archive cleanup dated.
7. **Flows/enforcement/runbook** (stub; own design later).

## Testing strategy

- **Unit tests** (inline snapshots, stub-on-PATH where scripts touch tools): the hardened
  unit file and argv; the resource `set-property` line; the label-based disk selection
  against a device table with a RO iso9660 disk; the per-box `-S` runtime/tmpfs overrides
  (gen-2 only); the rebuild provider's forwarding; the `oom-kill` journal match; migration
  039's backfill SQL against the fake store; the `workspace_migrating` guard (gen-1 + NULL
  placement -> 409; gen-2 or placed rows unaffected); same-generation candidate filtering;
  the subpackage import ratchet; the cutover renderers (gen-1 stop/upload script, fixed-port
  reserve, relabel script, container create args from a row, autostart installer) and the
  preflight parsers (`git describe` output, health probe output, fit computation with
  oversized disks); report rendering.
- **Integration tests** (connector fakes / mock SSH): stop/start on gen-2 with the
  conversion gone; a parked row is invisible to the watchdog predicate and the retention
  finalize; the start guard; the resize paths with NOT NULL columns.
- **Release tests** (CI gen-2 boxes): `test_machine_resize.py` (ungated in phase 2),
  `test_workspace_stop_start.py` (ungated in phase 5), the remote-workspace suite.
- **Rehearsals** (the migration's only proof): dev, repeatedly, then staging; a checklist
  per stage (dry-run output matches, reports complete, every workspace passes the probe,
  desktop shows the workspace running at the same address, `host_state.json` and agents
  intact, resize after migration works, pool restocked). Happy path only; failures handled
  as they come.
- **Benchmark**: the fixed script on runc vs runsc, report committed; knobs decided after.
- **Manual verification on the canary**: hardened unit boots and stops cleanly; discard
  reclaim still works; `journalctl -u mngr-slice@N` carries the console; telemetry
  integrity check passes after re-prep; `SLICE_UNIT_OOM_KILLED` fires when forced.

## Open questions

- Exact `SLICE_BOOT_DISK_GIB`: 20 GiB leaves ~9 GiB over the measured image for docker
  layer growth (agent apt installs live in the writable layer). Confirm on the canary with
  a busy workspace before fixing the constant; 24 still fits 14 machines (14 x 52 = 728 GiB).
- Gen-1 data disk format: lima's `_disks/<name>/datadisk` is assumed raw; verify on a gen-1
  box (`qemu-img info`) so the transplant's convert step is right.
- `git describe` inside the container needs the workspace's tags fetched; a workspace whose
  clone has no `minds-v*` tags (very old bake, shallow clone) describes nothing -- preflight
  refuses it; confirm how common that is when the first production preflight runs.
- Namespace-package mount -- RESOLVED 2026-08-28 (phase 3): a `modal run` against a
  throwaway function built with the connector's exact image and mount showed
  `imbue.mngr_imbue_cloud` importing as an implicit namespace package (`__file__` None,
  only `slices/gen2_scripts` under it), every `gen2_scripts` module and every connector
  module that uses them importing cleanly, and `imbue.imbue_common.logging` failing as
  intended (no loguru in the image). The subpackage stays; no separate `libs/slice_scripts`.
- `server setup` on a repave: confirm it re-records the box's sshd host key and that the
  connector row for the box needs nothing else (WireGuard identity is irrelevant while the
  lockdown is deferred).
- Resolved 2026-08-27: migration 039 stamps a gen-1 row with the size its data disk has
  after the cutover (the box-formula lima size plus the 16 GiB gen-2 base), not its current
  lima size, so the quota and `machines show` count the capacity the machine will have; the
  restore grows the transplanted disk to exactly that size.
- Does staging currently host user workspaces (affects the staging soak's realism)?
- The number of distinct workspace versions in production (drives how many image seeds the
  window pays per box); the first preflight report answers it.
- gVisor knobs: decided 2026-08-27 from the benchmark and tuning passes
  (`benchmark-summary-2026-08-27-tuning.md`): directfs on (default), `--overlay2=none`,
  netstack (no hostinet), default `shared` volume file access (no sentry metadata cache),
  4 vCPUs per 8 units (`DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO = 4.0`). The KVM platform
  (nested virt) stays off with the hardening.
- Phase 7's design (sampling rate, NFLOG layout, retention, promotion criteria) is deferred
  to its implementation.
