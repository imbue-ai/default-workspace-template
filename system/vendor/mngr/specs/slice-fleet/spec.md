# Slice fleet: variable machine sizing and the gen-2 completion

> **Status (2026-08-27):** phases 3-6 below (the CI split fleet, the staging/production
> turnover through the in-supervisor gen-1 -> gen-2 conversion, and its cleanup) are
> superseded by `blueprint/slice-fleet-cutover/plan-slice-fleet-cutover.md`, which cuts each
> tier over in one operator-driven window instead. The conversion, the migration release
> test, and the generation lease filter were deleted in that plan's phase 2, as were the
> lazy stop-time `disk_gb` backfill and every NULL-tolerant sizing fallback (migration 039
> backfilled `memory_units` / `disk_gb` and made both NOT NULL). The sizing design and
> phases 1-2 otherwise stand as written.

This spec supersedes `specs/slice-fleet-gen2/spec.md` (which keeps a pointer header and
remains as the historical record). It covers the variable-machine-sizing design plus
everything still unfinished from the gen-2 spec (its phases 5-9), so the old spec reads
as fully superseded.

Vocabulary follows `specs/machine-workspace-naming/decisions.md` (PR #436, soft reference,
no ordering dependency): the thing being sized is the **machine** (the slice VM -- CPUs, RAM,
disk -- identified by its mngr host id, stable across stop/start; the bare-metal box is
substrate). The **workspace** is the logical content that survives restores. mngr-level slice
code keeps host/slice vocabulary; new wire/CLI/entitlement surfaces are machine-named.

## Overview

- **Machines get variable sizes, measured in units.** A unit is 1GiB of guest RAM and is the
  single sizing knob: it drives RAM (`units` GiB), vCPUs (proportional share of the box's
  threads), and fair-share bandwidth (proportional HTB guarantee). Allowed sizes at launch
  are any multiple of 8 units from 8 up to 128 (8, 16, 24, ..., 128; in practice the
  doubling ladder 8/16/32/64/128 is what gets used); the architectural floor is 2 units
  (1 someday); sub-8 sizes are deferred.
- **Disk is a second, separate size factor -- grow-only.** A machine's data disk is sized once
  at carve (`DATA_DISK_BASE_GIB` 16 + `DATA_DISK_GIB_PER_UNIT` 3.5 per unit -> 44GiB at the
  8-unit default; the base covers the workspace docker image, which lives on this disk, plus
  the 4GiB system reserve outside the workspace quota) and thereafter decoupled from units: resizing
  units never changes disk, disk can be grown separately, and disk never shrinks.
  This also fixes a latent bug: today per-slice disk is *derived from the box*, so artifacts
  carved on big-disk boxes can silently overcommit a smaller-disk box on restore (thin qcow2
  passes the df guard, then ENOSPCs later). Explicit per-machine disk accounting closes it.
- **Two-budget box accounting replaces slot counting.** A box's capacity is checked as two
  independent budgets summed from the recorded per-slice env files under the existing
  allocation flock: memory units (`sum(units_i x 1024 + 512MiB per-VM overhead) <=
  (ram_gb - 8GiB reserve) x 1024`) and disk (`sum(20GiB boot + data_i) <= disk_gb -
  reserve`). The current uniform model is the special case (14 x 8-unit slices on the
  default 128GB box -- unchanged).
- **The pool stays uniform; sizing happens by resize-then-restart.** Every create leases a
  default 8-unit machine from the pre-baked pool (pool population stays simple). A resize is
  record-only: the server stamps the desired size; the machine picks it up at its next
  start. The gen-2 restore already re-renders the VM's sizing from server-side state per
  placement, so this rides the existing stop/start machinery.
- **Eviction makes room for large machines.** Unleased `available` pool rows are just
  pre-baked caches; the restore path may destroy them (two-pass: natural room first,
  eviction only when every candidate refuses) to fit a big machine. Replenishment stays
  manual (operator re-bake) with a metric.
- **Quotas meter units and disk, machine-scoped.** New entitlements
  `max_active_machine_units` and `max_total_machine_disk_gb` cap the billable resources;
  the existing workspace-count quotas stay.
- **The remaining gen-2 rollout proceeds on the final shape.** Variable sizing lands in
  full and is dev-verified *before* the canary (the urgent incident exposure is already
  mitigated separately), so the canary validates the final accounting/naming shape once;
  then CI split fleet, staging/production turnover, flows/enforcement, cleanup.
- **Gen-2 only.** Gen-1 rows refuse resize; migration to gen-2 (already the turnover
  mechanism) is how a machine becomes resizable.

Non-goals, explicitly deferred:

- Billing/metering for units or disk (quota caps only for now).
- UI controls for resizing (read-only display only; the CLI is the write surface).
- Automatic pool re-bake after eviction.
- Sub-8-unit sizes (2-unit architectural floor is designed for, not shipped).
- Storage add-on ordering (ordering validation *rejects* insufficient configs instead).
- Disk shrinking, in any form.
- Alert rules for the new failure-mode metrics (metrics only until usage shows thresholds).
- Everything the gen-2 spec already deferred (SEV-SNP, PerSourcePenalties, Falco, gen-1
  lockdown backport, synthetic-abuse tests, `mngr_lima` changes).

## Expected behavior

User-visible:

- A new workspace is created exactly as today: an 8-unit machine with a 44GiB data disk.
- `mngr imbue_cloud machines resize <host> --units N [--disk-gb M]` stamps the desired
  size after validating quota and the allowed-size set. Nothing changes until the machine
  next starts; the CLI says so.
- `mngr imbue_cloud machines show [<host>]` renders current/target units and disk, machine
  state, and a "restart to apply" flag.
- The desktop client shows the machine's current size and a passive "restart to apply this
  machine's new size" note when a target is pending. No UI controls to change it.
- Restarting (stop, then start) applies the pending size: in place when the origin box has
  room, otherwise via a restore -- possibly onto a different box, transparently.
- Units may go up or down (any multiple of 8 from 8 to 128). Disk only grows; a
  shrink request is refused with a structured error. Setting targets equal to current
  clears pending targets (idempotent).
- Resize requests are accepted from every machine state except `starting` (refused with the
  current status -- an in-flight supervisor may be reading the targets); `crashed` rows
  accept (it is just database fields).
- A size the fleet cannot place right now fails the start gracefully: the machine lands
  back on `stopped` with a clear "not possible right now, try a smaller size or try again
  later" error, and a metric fires. No artificial cap below 128.
- A grown disk appears inside the workspace automatically after the restart (the guest
  grows its filesystem on boot); the container's memory cap follows the machine's RAM
  automatically.
- Quota rejections are the standard structured 403 (`quota_exceeded` with entitlement,
  limit, current).

Operator-visible:

- `minds-admin pool create --units N` can bake odd-sized slices for dev/testing (default
  8; production bakes stay conventional).
- `POST /admin/machines/{host_db_id}/resize` (admin key) skips quota but still validates
  the allowed-size set and the disk-shrink refusal.
- Eviction events and resize-placement failures are visible as connector metrics; pool
  depth after eviction is the operator's re-bake signal.
- `server order` / `register` refuse a box config whose storage cannot hold a full
  complement of default-size machines at the disk factor; the pricing table gains a
  "units-valid" indicator per row.
- The remaining gen-2 operator flows (canary, drain, repave, WireGuard, telemetry) are unchanged
  from the old spec, now exercised on the sizing-aware shape.

System behavior:

- The lease path is untouched: attributes matching, fast/slow path, adoption all work as
  today (the pool is uniform; bake-stamped attributes remain the matching document).
- Every successful start or carve restamps the row's current size columns and clears
  applied targets; a restore at a target size IS the resize application.
- Restart-in-place with a pending resize re-checks capacity under the box flock, rewrites
  the env file, resizes the data disk qcow2, and restarts the unit; if the box lacks room
  (after an origin-box eviction attempt), the start falls back to the restore path.
- Gen-1 -> gen-2 conversion restores stamp the machine's actual measured sizes; gen-2
  restores of rows with unknown (NULL) disk size their df guard from the artifact's
  recorded object metadata rather than the default.

## Implementation plan

### Sizing model and constants (libs/mngr_imbue_cloud)

- `slices/bare_metal.py`:
  - `DATA_DISK_GIB_PER_UNIT: Final[float] = 3.5`, `DATA_DISK_BASE_GIB = 16`,
    `DATA_DISK_SYSTEM_RESERVE_GIB = 4`, and `GEN2_BOOT_DISK_GIB = 10` next to the gen-1
    `SLICE_BOOT_DISK_GIB` (20; gen-1 keeps docker on its boot disk). Gen-2 guest layout
    (decided 2026-08-27): docker's data-root (`/mnt/mngr-data/docker`) and containerd's root
    (`/mnt/mngr-data/containerd`; Docker's image store, so image and container layers live in
    its overlayfs snapshotter) are on the data disk; first boot enables btrfs *simple* quotas
    (extents charged to their creating subvolume, so snapshots never leave the accounting
    inconsistent) and creates `containerd/io.containerd.snapshotter.v1.overlayfs` and
    `containerd/io.containerd.content.v1.content` as subvolumes in qgroup `1/0`; the per-host
    home subvolume joins the same group at creation (`mngr_vps`
    `HOST_QUOTA_QGROUP`); the grow oneshot limits the group to `filesystem - 4GiB` on
    every boot, so a resize grows the quota. Docker's metadata, the backup snapshots and btrfs
    metadata stay outside the quota, so a full workspace never stops dockerd or the snapshot
    helper. The guest image's daemon.json carries the data-root and json-file log rotation,
    `/etc/containerd/config.toml` carries containerd's root, both engine units get a
    `RequiresMountsFor=/mnt/mngr-data` drop-in (and lose their boot enablement; first boot
    starts them after the mount), and journald gets `SystemMaxUse=512M`.
  - `DEFAULT_MACHINE_UNITS: Final[int] = 8`; `MACHINE_UNITS_STEP: Final[int] = 8`;
    `MAX_MACHINE_UNITS: Final[int] = 128` (allowed = any multiple of the step in
    [step, max]).
  - `compute_box_total_units(ram_gb)` = the box's sellable unit budget
    (`(ram_gb - HOST_RAM_RESERVE_GIB) x 1024` MiB, consumed as `units x 1024 + 512` per
    machine -- the existing overhead constants, reused).
  - `compute_machine_vcpus(cpu_threads, overcommit, units, total_units)` =
    `min(cpu_threads, max(1, floor(threads x overcommit x units / total_units)))`.
    `DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO = 4.0` (was 2.0): an 8-unit machine on a
    16-thread / 120-unit box gets 4 vCPUs. gVisor's systrap platform hands every syscall
    between the application thread and a sentry thread, which starves a 2-vCPU guest; the
    unit's proportional `CPUWeight` still governs the real share (decided 2026-08-27, see
    `blueprint/slice-fleet-cutover/benchmark-summary-2026-08-27-tuning.md`).
  - `compute_machine_data_disk_gib(units)` = `ceil(units x DATA_DISK_GIB_PER_UNIT)`.
  - (Superseded 2026-08-28, cutover phase 4: the gen-2 disk budget is no longer "usable disk
    minus the 10% / 20 GiB reserve". It is `compute_gen2_disk_budget_gib(disk_gb)` = the box's
    MEASURED XFS storage partition (recorded as the row's `disk_gb` by the gen-2 prep) minus
    `GEN2_STORAGE_RESERVE_GIB` = 64 (32 swapfile + 16 image tar cache + 4 base image + 12
    margin, all on the storage partition); the pre-delivery ordering guard estimates the
    partition as `usable - 20 GiB root - 1 GiB boot`
    (`compute_gen2_storage_partition_estimate_gib`). See
    `blueprint/slice-fleet-cutover/phase-4-cutover-tooling.md` section 7.)
  - Existing `memory_per_slice_gb`/`slot_count` functions stay for gen-1; gen-2 paths stop
    consuming them (removed at cleanup).
- `primitives.py`: `MachineUnits` (validated against the step/max rule at the wire/CLI
  boundary; internally a positive int so the 2-unit floor is a config change later).

### Gen-2 renderers (libs/mngr_imbue_cloud/slices/qemu_slice.py)

(Since cutover phase 3 the env-file renderer, the two-budget guard and ordinal-derivation bash
blocks, and the cloud-init material with its sizing oneshots live in the shared
`libs/mngr_imbue_cloud/.../slices/gen2_scripts/` subpackage (`box_commands.py`, `guest.py`), which
also ships into the connector container; `qemu_slice.py` keeps the unit / helper / sudoers and the
carve reserve script.)

- `GEN2_MAX_SLICE_COUNT` 64 -> 512. Prep pre-creates 512 users; sudoers renders 512 x 5
  exact-argument lines; `/30` derivation, MAC, tc mark, and tc class-minor encodings are
  verified for the range (all fit).
- Env file schema: `MNGR_SLICE_UNITS` and `MNGR_SLICE_TOTAL_UNITS` replace
  `MNGR_SLICE_SLOT_COUNT`; add `MNGR_SLICE_DATA_DISK_GIB`. `MNGR_SLICE_MEMORY_MIB` is the
  guest's boot RAM: `units x 1024 - GUEST_RAM_HOLDBACK_MIB` (512), so that guest RAM plus
  qemu's own memory fit under the unit's `MemoryMax`, which is exactly the machine's budget
  share `units x 1024 + PER_VM_RAM_OVERHEAD_MIB` -- a full box's caps sum to its budget and
  the box is never overcommitted (decided 2026-08-27). The qcow2 drives run `cache=none,
  aio=native` so host page cache for the images does not count against the cap.
- Helper script (`render_slice_helper_script`): HTB guarantee becomes
  `uplink x units / total_units` (min 1mbit); everything else (ceilings, counters,
  anti-spoof, SMTP block) is unchanged -- abuse ceilings stay flat per machine.
- Reserve script (`build_qemu_reserve_script`):
  - Capacity guard: replace the instance-dir count with the two-budget sum read from the
    recorded env files under the flock (units budget and disk budget); distinct refusal
    markers `MNGR_SLICE_NO_UNITS` and `MNGR_SLICE_NO_DISK` (BOX_FULL retired for gen-2
    carves; NO_SPACE df guard stays as the last-line real-free-space check).
  - Payload optimization: ship ONE cidata/env template with placeholders for the ordinal
    and its derived values (vm_ip, gateway_ip, mac); the box-side script computes them from
    the chosen ordinal (`10.201.0.0 + 4xN` arithmetic in bash) and substitutes -- replacing
    the per-candidate-ordinal base64 case tables, which do not scale to 512.
- Cloud-init user-data (`build_qemu_slice_user_data`): install two root-owned every-boot
  systemd oneshots in the guest, ordered before docker:
  - `mngr-grow-data-fs`: idempotent `btrfs filesystem resize max` on the data mount.
  - `mngr-reconcile-container-memory`: computes the cap from the VM's own visible RAM
    (total - 1GiB reserve) and `docker update --memory --memory-swap`s the workspace
    container when it exists and differs. Covers both restore and in-place resize paths
    with no management-plane access; templates can override either unit.
- Provider (`providers/slice_provider.py`, `providers/rebuild.py`): carve sizing flows from
  units (vcpus/memory/disk via the new functions); the slow-path container memory args
  derive from the lease's `memory_units`.

### DB schema (apps/remote_service_connector/migrations/)

- Migration 036: `pool_hosts` gains `memory_units INTEGER`, `target_memory_units INTEGER`,
  `disk_gb INTEGER`, `target_disk_gb INTEGER` (all nullable). Backfill
  `memory_units` from `attributes->>'memory_gb'` (1 unit = 1GiB). `disk_gb` stays NULL =
  "unknown" (lazy backfill below). (Superseded: migration 039 backfilled `disk_gb` and made
  both sizing columns NOT NULL.)
- Migration 036 (or sibling): `plans` / `account_entitlements` gain
  `max_active_machine_units` (free 8 / explorer 16 / ally 80) and
  `max_total_machine_disk_gb` (free 140 / explorer 280 / ally 1400); per-tier `deploy.toml`
  `[plans]` blocks updated (git-owned plan defaults).

### Connector (apps/remote_service_connector/)

- `machines.py` (new router, machine-named route family):
  - `POST /machines/{host_db_id}/resize` (SuperTokens auth): body
    `{target_memory_units?, target_disk_gb?}`. Validates ownership; allowed-size set;
    disk never shrinks (`target_disk_gb < current` -> structured 400); refuses state
    `starting` (409 with current status); quota checks (below); stamps targets; targets
    equal to current clear pending targets.
  - `POST /admin/machines/{host_db_id}/resize` (admin key): same body/validation, skips
    quota.
  - Quota semantics: `max_active_machine_units` counts running statuses
    (`leased`/`stopping`/`starting`) -- a resize counts this machine at its target;
    starting a stopped machine re-checks (its units re-enter the active sum).
    `max_total_machine_disk_gb` counts data-disk GB across running + stopped -- the resize
    request is the grant point (disk never changes at stop). Never-revoke policy: lowering
    a quota below usage refuses new grants/starts, never kills running machines.
- `workspaces.py`: GET responses additively expose `memory_units`, `target_memory_units`,
  `disk_gb`, `target_disk_gb` (WireModel rules; absent on old servers).
- `hosts.py`: lease response carries the machine's sizes (additive wire fields);
  bake insert (`/hosts` registration from `pool create`) stamps `memory_units`/`disk_gb`.
- `stop_start.py`:
  - `_gen2_restore_slice_sizing` reads the row's target (falling back to current) columns
    instead of attributes; a successful start restamps current = applied size, clears
    targets, and updates `attributes` memory/cpus mirrors. (Superseded: cutover phase 2
    dropped the `attributes` mirror updates; the start only restamps the sizing columns and
    clears the targets.)
  - The stop upload records the actual qcow2 virtual sizes (boot + data) in the artifact
    manifest and restamps `disk_gb` -- the lazy backfill for pre-existing rows.
    `# CLEANUP:` marker: tighten `disk_gb` to NOT NULL (and drop the NULL-tolerant df
    guard fallback) once every gen-2 row has cycled a stop. (Superseded: cutover phase 2
    settled this CLEANUP with migration 039; the upload still records the sizes in the
    manifest, but nothing restamps `disk_gb` from them.)
  - Restore df guard sizes from the machine's disk (target if grown, else recorded, else
    the artifact's manifest sizes for NULL-disk rows) instead of `disk_gb // slot_count`.
    (Superseded: with the columns NOT NULL there is no NULL-disk fallback.)
  - Restore-reserve renders at the target size (env template with the machine's units,
    total units from the candidate box, data-disk GiB) and, for a disk grow,
    `qemu-img resize`s the downloaded data disk before boot.
  - Restart-in-place with pending targets: a new caller-rendered box script (under the
    allocation flock: re-sum both budgets excluding this instance, refuse with the
    NO_UNITS/NO_DISK markers, rewrite the env file, `qemu-img resize` the data disk) runs
    before the unit restart; on refusal, try an origin-box eviction pass, then fall back
    to `_restore_from_artifact`.
  - Eviction (two-pass) in `_restore_from_artifact`: pass 1 tries every candidate box
    as today; when all refuse for capacity, pass 2 ranks boxes by fewest evictions needed
    (origin box first for in-place fallbacks), destroys just enough unleased `available`
    gen-2 rows there (existing teardown machinery, statuses CAS'd so a concurrent lease
    cannot grab a row mid-destroy), emits the eviction metric, and retries the reserve.
    When even eviction cannot fit the size: fail the start back to `stopped` with the
    "not possible right now" `transition_error` and the placement-impossible metric.
- `box_scripts_gen2.py`: mirrors of the new env schema, markers, budget guard, placeholder
  substitution, and the in-place resize script -- covered by the existing drift ratchet. (Superseded: cutover phase 3 replaced the connector's mirror and its drift ratchet with the shared `libs/mngr_imbue_cloud/.../slices/gen2_scripts` subpackage, mounted into the connector container.)
- Metrics (modal_app_kit `metric` lines): `machine_resize_recorded`,
  `machine_resize_placement_impossible`, `pool_rows_evicted`.

### Plugin CLI and wire (libs/mngr_imbue_cloud)

- `wire_types.py`: additive `memory_units`/`target_memory_units`/`disk_gb`/`target_disk_gb`
  on lease + workspace wire models (defaults None against older connectors).
- `cli/`: `mngr imbue_cloud machines resize <host> --units N [--disk-gb M]` and
  `mngr imbue_cloud machines show [<host>]` (renders current/target sizes, state, and the
  restart-to-apply flag from the workspaces listing). Wire vocabulary carve-outs already
  permit machine/workspace terms here.

### Operator tooling (apps/minds_admin)

- `cli/pool.py`: `--units N` bake override (default 8, help text marks it dev/testing-only);
  carve sizing threads through the plugin's new unit-based functions; bake stamps
  `memory_units`/`disk_gb` on the inserted row.
- Bake ordering (row first): the bake chooses the slice's host id, inserts its `pool_hosts`
  row as `baking` (identity, sizing and the derived instance/disk names; agent id, forwarded
  ports and sshd host keys NULL -- migration 038), then carves via
  `-S providers.imbue_cloud_slice.slice_host_id=<id>`; success flips the row to `available`
  with the bake result, failure deletes it after the VM rollback. The post-bake orphan reap
  (also `pool reap-orphans`) treats any row -- `baking` included -- as tracked, and never
  touches a rowless VM that is running or younger than `ORPHAN_SLICE_MIN_AGE_SECONDS` (2 h),
  nor the data disk of a running or spared VM. `pool destroy` claims a `baking` row only once it is
  older than that threshold.
- `slices/bare_metal_prep.py`: 512 pre-created users; the sudoers/unit artifacts re-render
  (version markers converge existing boxes on re-prep).
- `slices/ordering.py` + `cli/server.py`: `server order`/`register` validate
  `usable_disk >= reserve + slot_capacity x (boot + DATA_DISK_GIB_PER_UNIT x 8)` for the
  box's unit budget and REFUSE insufficient configs (no storage add-on ordering); pricing
  table (`slices/pricing.py`) gains a "units-valid" column.
- `server list` capacity display becomes unit-based for gen-2 boxes (used/free units and
  disk) while keeping slot display for gen-1.

### Desktop client (apps/minds, read-only)

- Workspace settings surface shows the machine's current size and a passive "restart to
  apply" note when a target is pending (data from the existing workspaces polling; additive
  fields tolerated by the wire models).

### Docs

- New spec `specs/slice-fleet/spec.md` (this content); pointer header on
  `specs/slice-fleet-gen2/spec.md`.
- Glossary: "machine size" entry (units + disk, the resize-at-restart contract).
- `libs/mngr_imbue_cloud/README.md`: machines resize/show commands, sizing model.
- `apps/minds/docs/deploy/host-pool-setup.md`, `reference/workspace-stop-start.md`: two-budget
  accounting, eviction, ordering validation.
- Carried from the old spec: `gen2-turnover.md` and `abuse-response.md` runbooks (still to
  be written, in their phases below).

## Implementation phases

Each phase lands as one or more PRs and leaves the system working.

1. **Variable machine sizing, end to end on dev.**
   Everything above: sizing model + constants, renderer/env/reserve changes with the
   512-ordinal ceiling and payload optimization, in-guest oneshots, migration 036,
   connector resize/quota/eviction/backfill machinery, wire + CLI, bake `--units`,
   ordering validation, read-only UI, docs, and the dedicated release test -- authored
   here and exercised locally against a dev gen-2 box (`just test <path>::<test>`) until
   the CI tier has a gen-2 box.
   Exit: on dev -- create at default, resize up in place, resize via restore with
   eviction, disk grow lands in-guest, container cap follows, quotas enforce, placement
   failure surfaces cleanly.
2. **Dev canary** (old phase 5, extended).
   One dev box ordered or repaved as gen-2 through the real flows; the full old checklist
   (bake, lease, workspace use, stop/start round-trip, gen-1 artifact migration with
   upgrade, drain, detection signals by hand) PLUS the five sizing items: in-place resize
   up (VM + container RAM, vCPUs, HTB verified), resize via restore with two-pass eviction
   (metric observed, rows destroyed, re-bake after), end-to-end disk grow, the
   placement-impossible refusal + metric, and a pre-existing row's lazy disk backfill.
   The canary is also the ground truth for everything the old spec marked
   reasoning-derived: the per-VM nftables/tc ruleset paths and the conversion's one-time
   replay + data-disk remount.
3. **CI split fleet** (old phase 6).
   One gen-2 box joins the CI tier alongside gen-1; the remote-workspace release suite --
   now including the resize release test (`test_machine_resize.py`, ungated in cutover
   phase 2; the migration release test was deleted with the conversion) -- runs against
   both generations for the remainder of the rollout. `sweep_ci_slices_on_box` learns
   gen-2. (Superseded: the generation lease filter this phase relied on -- an exact-match
   `box_generation` lease field with a `-b generation=<n>` plugin knob -- was removed in
   cutover phase 2; the cutover plan runs the release suite on gen-2 boxes only.)
4. **Staging, then production turnover** (old phase 7).
   Redeploy tier services; seed two gen-2 boxes per production region (one for staging)
   ordered through the new units-validated flow; then per batch: `server drain` ->
   workspaces restore onto gen-2 (converted + upgraded) -> `server repave` -> box rejoins
   as gen-2 capacity; retire old `available` rows promptly; staging rehearses the exact
   production sequence.
   Per-tier prerequisites, learned on the dev canary: (a) the tier's Modal Proxy --
   lookup is environment-scoped and a workspace's proxy-IP limit is 1, so the proxy
   lives in ONE Modal environment named by `[modal_proxy].environment_name` (creatable
   via the Modal Proxy API; no dashboard needed); (b) operator tooling must reach
   locked-down boxes over the WireGuard overlay (the bake/prep-tooling item in the
   slice-fleet handoff) -- land it before the first staging lockdown; (c) arm the
   tier's alerting by copying the shared `mngr-openobserve-alerts` GitHub token into
   the tier's `OBSERVABILITY_ALERTS_GITHUB_TOKEN` Vault leaf and running
   `provision-alerts` against the tier's instance (every tier posts to the same repo,
   so one token serves all; rotation therefore touches every armed tier).
5. **Flows, enforcement promotion, runbook** (old phase 8).
   pmacct sampled flows (Debian packages, nftables `log group`, per-VM per-destination
   records into the pipeline, ~30-day operator-only retention); the abuse-response runbook
   (`apps/minds/docs/deploy/abuse-response.md`); per-rule promotion of alert-only rules to
   enforcement as their histories come back clean; the `gen2-turnover.md` runbook.
6. **Cleanup** (old phase 9, extended).
   After the last gen-1 box and artifact are gone: delete `lima_slice*.py`, the gen-1
   box_scripts variants, `key_repair.py`, the gen-1 prep/autostart machinery, and the
   lima-format restore path; shrink the adoption reconciler to rotation-only; remove the
   `memory_per_slice_gb`/`slot_count`-based gen-2 paths and gen-1 columns' gen-2 use;
   settle the disk-backfill CLEANUP (NOT NULL + drop the NULL-tolerant guard -- already
   done in cutover phase 2 by migration 039); sweep every `CLEANUP:` marker this program
   introduced.

## Testing strategy

- **Unit tests** (inline snapshots, existing patterns): the two-budget capacity math and
  overhead accounting; ordinal 512 derivations (tap/user/MAC//30/tc encodings at the
  boundaries); the placeholder-substituting reserve/restore scripts; env-file rendering
  with units/total-units/disk; the in-guest oneshot units' rendered content; vCPU and HTB
  proportionality including the thread-count cap and 1-vCPU/1mbit floors; allowed-size and
  disk-shrink validation; quota arithmetic (target-replaces-current, running-only units,
  running+stopped disk).
- **Drift ratchet**: the connector's copies of the new env schema, markers, budget guard,
  and resize script stay pinned byte-for-byte to the plugin's source. (Superseded: cutover phase 3 replaced the connector's mirror and its drift ratchet with the shared `libs/mngr_imbue_cloud/.../slices/gen2_scripts` subpackage, mounted into the connector container.)
- **Integration tests** (fake stores / mock SSH transport): resize endpoint state matrix
  (every state accepts except `starting`; crashed accepts; idempotent clears); restamp-on-
  start and target-clearing; two-pass eviction ordering (natural room first, fewest-
  evictions ranking, origin-box preference, unleased-only, concurrent-lease CAS); in-place
  resize refusal -> origin eviction -> restore fallback sequencing; wire compat via the
  golden old-client snapshot test. (Superseded: the lazy disk backfill and the NULL-disk df
  guard fallback, and their tests, were deleted in cutover phase 2.)
- **Release tests**: the dedicated resize test (create at default, resize up via the
  endpoint, stop/start, assert the VM and container see the new memory and the disk grow
  landed, workspace healthy). (Superseded: the migration release test that exercised the
  in-supervisor conversion, `test_machine_migration.py`, was deleted in cutover phase 2;
  the cutover's proof is the plan's rehearsal checklist.)
- **Manual canary verification** (phase 2): the extended checklist above -- deliberately
  manual, no synthetic-abuse automation.
- **Edge cases to cover**: reserve racing under the flock with mixed sizes; NO_UNITS vs
  NO_DISK vs NO_SPACE refusal precedence; eviction finding zero destroyable rows;
  a 128-unit request on a fleet that can never fit it; unit shrink (16 -> 8) leaving disk
  untouched; resize during `stopping` applying at the eventual start; targets stamped then
  quota lowered (start refused, machine stays stopped, data intact); a pre-sizing gen-2 VM
  (no oneshots) gaining them at its next cross-placement restore.

## Open questions

New:

- **Pre-sizing gen-2 VMs and in-place resize**: the in-guest oneshots arrive via cloud-init
  at carve or cross-placement restore; a VM carved before this work that is resized
  strictly in place would grow its qcow2 without the in-guest grow/cap-update. Only dev
  gen-2 VMs exist today, so the plan assumes re-carve/restore covers them -- confirm no
  such VM needs in-place-only treatment.
- **Eviction vs concurrent restores**: two restores evicting on the same box serialize on
  the reserve flock but not on the eviction destroys; the CAS on row status should make
  this safe -- verify the interleaving in the integration tests, and decide whether pass 2
  needs a connector-side advisory lock per box.
- **The allowed-size rule (step/max) as config vs constant**: launch ships it as plugin constants;
  if plan tiering later wants per-plan size menus, it moves into entitlements.

Inherited from specs/slice-fleet-gen2 (still open, carried so the old spec reads as fully
superseded):

- **The per-VM nftables/tc ruleset is reasoning-derived, not live-tested** -- RESOLVED by
  the phase-2 canary (2026-08-24): every path verified live (external SSH via DNAT,
  box-local image-cache transfer over loopback, guest egress, hairpin, the
  guest-to-management block, SMTP block, and the ceilings).
- **Modal Proxy in deployed shape** -- RESOLVED on the dev canary: connector functions
  attached to the proxy egress from its static IP (verified live), and the post-lockdown
  stop/start cycle proves the paramiko box SSH rides it. Two constraints discovered:
  proxy lookup is Modal-environment-scoped and the workspace proxy-IP limit is 1, so the
  shared dev proxy lives in the `main` environment and is resolved by name + environment
  (`[modal_proxy].environment_name`), with the proxy env vars forwarded into the
  containers (a Proxy is a function dependency and must evaluate identically at deploy
  time and in-container).
- **Upgrade duration budget** -- RESOLVED on the dev canary: a thin workspace's full
  conversion restore (download + first boot + dist-upgrade + reboot + verification) took
  ~6 minutes; the shipped 1-hour poll bound has ample margin and stands.
- **The conversion's one-time replay and data-disk remount are reasoning-derived** --
  RESOLVED by the canary's migration run (host key byte-identical, container back up
  post-upgrade, data remounted with content intact), which also caught and fixed the
  partitioned-disk mount, the lima per-boot hook leak, and the partition-unaware disk
  grow (see PR #574).
- **pmacct sampling rate and NFLOG group layout**: pick during the flows phase with real
  traffic volumes in hand.
- **OpenObserve -> GitHub webhook arming**: the dev tier's stored destination carries a
  placeholder token; arming delivery means re-running `provision-alerts` with a dedicated
  issues-write PAT (and SMTP remains unprovisioned as the fallback channel).
- **Debian 12 LTS horizon (~June 2028)**: gen-1 boxes and their bookworm guests persist
  until turnover completes; the turnover schedule should finish well before the horizon,
  and the runbook should note it.
