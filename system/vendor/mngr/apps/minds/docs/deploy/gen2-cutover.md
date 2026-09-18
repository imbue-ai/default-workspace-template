# Gen-1 -> gen-2 incremental migration runbook (`minds-admin cutover`)

Per tier: gen-1 (lima) workspaces move onto gen-2 (raw qemu, runsc) slices one
at a time, each onto an operator-named gen-2 target box at a new address and
freshly picked ports (the client re-resolves; harvested host keys mean no
trust prompt). There is no per-tier window: boxes empty as their workspaces
migrate off, are repaved gen-2, and are baked with minds-v0.6.0+ rows. The
tooling lives in `apps/minds_admin/imbue/minds_admin/cli/cutover*.py` and
`slices/cutover_*.py` and is deleted after the last gen-1 row is gone (phase 6
of `blueprint/slice-fleet-cutover/`). Design:
`blueprint/slice-fleet-cutover/phase-5.5-incremental-rollout.md`.

Four commands, run with the tier's env activated (`minds-admin env activate
<env>`), all re-runnable from the state dir `~/.minds-<env>/cutover/`:

```
minds-admin cutover preflight [--server-id ID ...] [--json-out PATH]
minds-admin cutover migrate   --yes-i-mean-<tier> --target-server-id ID \
                              [--workspace ID ...] [--user EMAIL] [--source-server-id ID] \
                              [--keep-origin-vm] [--publish-image-tars] [--dry-run]
minds-admin cutover rollback  --yes-i-mean-<tier> --workspace ID
minds-admin cutover repave    --yes-i-mean-<tier> --server-id ID [--server-id ID ...] [--dry-run]
```

`<tier>` is `dev`, `staging` or `production`; the flag must name the activated
env's tier (a `ci-*` env, such as the `ci-infra` scaffolding the standing CI
boxes are operated from, is guarded by `--yes-i-mean-dev`). Every command
writes `reports/<stage>-<timestamp>.{json,txt}` to
the state dir and exits non-zero when anything failed. The migrate's stop is
a `maintenance` hold (`specs/workspace-stop-kinds.md`): from the moment it is
requested, and until the finish CAS re-leases the row, the owner's
`POST /workspaces/{id}/start` answers `409 workspace_under_maintenance` ("This
machine is undergoing maintenance and will be back shortly."), a 0.6.1+ desktop
shows the machine as "Maintenance" with no Start control, and no desktop's
unattended recovery starts it (an older desktop that tries gets the same
refusal, one failure card, and no start). A migration abandoned after the
stop but before the park is handed back to its owner with
`minds-admin workspaces set-stop-kind <id> idle`; once the row is parked
(placement and artifact manifest cleared), the connector's parked-row guard
refuses the owner's start whatever the kind and there is no artifact for the
product's restore, so only `cutover rollback` (or a re-run of the migrate)
brings it back. The migrate refuses to run against a connector that predates
stop kinds (migration 042).

## How each cohort moves

- **New workspaces** move via the release channels: 0.6.x+ releases pair with
  gen-2 boxes and 0.5.x-and-older with gen-1 (a bake-time guard keeps the
  pools disjoint per tag; a `max_box_generation` lease field keeps pre-0.6
  clients off gen-2 rows even on the slow path). Promoting a 0.6.x build
  through alpha -> beta -> stable widens the cohort. Once a tier's gen-1
  stock is fully retired, a pre-0.6 client's create answers a clear
  update-the-app error.
- **Existing workspaces** move via `cutover migrate`, one at a time: start
  with a single alpha workspace, verify, then widen (per user, then per
  source box).

## Prerequisites (per tier)

- The release carrying this tooling is deployed to the tier (connector with
  the parked-row `workspace_under_maintenance` guard, the `max_box_generation`
  lease filter, and the admin start/stop endpoints; migration 039 has stamped
  every gen-1 row's `disk_gb`).
- At least one ready gen-2 box in the workspace's region (the beachhead):
  order a fresh box for production; on dev/staging, repave a box that holds
  zero pool rows (destroy its `available` rows via `minds-admin pool destroy`
  first).
- The tier's management-plane lockdown is live before its first gen-2 box
  takes workspaces, in the order
  [gen2-management-plane.md](./gen2-management-plane.md) gives: create the
  tier's Modal proxy in the Modal environment named by
  `[management_plane.modal_proxy].environment_name`; commit the
  `[management_plane]` table of
  `apps/minds/imbue/minds/config/envs/<tier>/deploy.toml` (the operators'
  WireGuard public keys and the proxy name + static IPs; today only `dev/`
  has one); `minds-admin env deploy` the connector so it egresses from
  the proxy; then `server prep` / `setup` the gen-2 boxes, which installs the
  `:22` lockdown. Gate: imbue-ai/mngr-internal#850 (SSH certificates from
  Vault) has landed before any tier locks down, so no locked-down box ever
  authorizes a fleet-wide key. DHCP placement (#849) and the artifact mirror
  (#851) are re-prep-safe and are not gates for the lockdown (the #849
  re-prep is its own prerequisite, next).
- Every gen-2 box has been re-prepped from a checkout carrying #849 (DHCP
  placement) before any workspace restores onto it: the prep installs the
  slice DHCP server (`mngr-slice-dhcp.service`, dnsmasq bound to the slice
  taps) and the helper's DHCP accept, and a slice carved or restored by the
  #849 code gets no address without them. Running slices are unaffected by
  the re-prep (their static addressing keeps working until they are
  re-carved). Gen-2 slices carved before #849 carry a static netplan for
  their original ordinal, so their artifacts cannot be restored onto a
  different ordinal or box; only dev-tier gen-2 VMs predate it -- destroy and
  re-bake their `available` rows, re-create their leased workspaces, and
  release their `stopped` rows rather than expecting those to restore.
- The artifact mirror holds every pinned artifact (`minds-admin artifacts
  verify` is clean) and the committed apt-mirror cut covers the `docker`
  archive (`apt-mirror verify` is clean): a gen-2 prep downloads everything
  from `apt.imbuepackages.com` and has no upstream fallback.
- The slice identifiers carry their generic names (#848): connector migration
  041 has been applied (the `slice_service_user` / `slice_instance_name` /
  `slice_disk_name` columns; the `lima_*` ones stay dual-written until the
  CLEANUP), and every gen-2 box runs as `slicehost`. A dev gen-2 box prepped
  before the rename is converged by one `minds-admin server prep --server-id
  <id>` (the prep re-owns `/srv/mngr-slices`, removes `limahost`, and stamps
  the row) -- run it while no stop/start on that box is in flight, since the
  connector SSHes as the row's recorded user and the old user loses its sudo
  grants mid-prep. Staging and production gen-2 boxes are repaved fresh and
  never carry the old user.
- Alerting is armed (box health, watchdog) and someone is watching Bugsink.
- If this operator machine ran the earlier flag-day drills (drain/restore) or
  the pre-rename migrate drills (whose per-workspace records still spell the
  instance and disk names `lima_*`), clear its state dir
  (`~/.minds-<env>/cutover/`) first: those records use a schema this tooling
  cannot parse, and every command reads them. Old leftovers mean no
  current-format migration is in flight, so clearing the whole dir is safe
  then -- never mid-migration.
- The workspace version floor is `minds-v0.3.10`: older workspaces are refused
  by the preflight. Ask their owners to run `update-self`, or accept losing
  them. A workspace created from a branch rather than a release tag holds no
  tags; its version is then read from its vendored mngr's `FALLBACK_BRANCH`.
- Every workspace must be on the `home/` volume layout: the migrate transplants
  the data disk only, and a slow-path-rebuilt workspace on the legacy layout
  keeps `/home/user` in the container's writable layer, so it would come back
  empty. Run `minds-admin repair-home-layout --all-leased` (probe) on the tier
  and `--host-id <id> --migrate` each `legacy_layout` workspace on its gen-1
  box first; the preflight and the harvest refuse a legacy layout and name
  this remedy. Staging had three such workspaces out of eight (2026-09-15).
- Templates before `minds-v0.5.0` bake only through the migrate's image seed
  (they name settings fields today's mngr renamed, and create a boot chat the
  pool bake refuses); the seed tolerates both. A gen-1 row that was stopped on
  no box when migration 039 ran carries `disk_gb = 44` (039's fallback); the
  migrate restamps it from the measured disk at the harvest.
- Migration announcement (per cohort, not per tier): the workspace stops,
  moves, and comes back at a new address on its own; expect minutes to tens of
  minutes of downtime depending on data size; afterwards the container runs
  under gVisor (ptrace/perf/eBPF/FUSE/io_uring and nested container runtimes
  stop working; filesystem-metadata-heavy operations are several times
  slower). Anything installed outside `/home/user` that env-converge did not
  record is gone, exactly as after a slow-path rebuild.

## Migrating workspaces

1. `minds-admin cutover preflight`: the tier's gen-1 inventory. Every row is
   `CANDIDATE` (leased, or stopped -- the migrate admin-starts those first),
   `DESTROY` (unleased rows for `pool destroy`) or `REFUSED` with its remedy
   (transitions in flight, crashed/removing rows). The `versions:` line names
   the image tars the migrations will publish.
2. `minds-admin cutover migrate --yes-i-mean-<tier> --target-server-id <gen2-box>`
   with one or more selectors: repeatable `--workspace <host_db_id>`,
   `--user <email>` (all their gen-1 workspaces), `--source-server-id <gen1-box>`
   (the box-emptying sweep). `--dry-run` prints the plan. Per workspace, in
   order, stopping at the first failure:
   - a soft capacity check on the target, so an obviously full box refuses
     before the workspace is touched (the reserve script on the target is
     the authoritative guard);
   - the invocation probes the connector for stop kinds (refusing on a
     connector without migration 042): up front against a running candidate
     when there is one, and again right before each product stop;
   - admin-start it when stopped, then live-harvest its SSH keys, container
     `docker inspect`, `git describe` version, and machine-owned latchkey
     state (the VM root's `~/.latchkey` files, the gateway and tunnel
     supervisord drop-ins, and the tmpfs gateway secrets, kept 0600 in the
     state dir until the workspace is re-leased);
   - the product's own admin stop as a `maintenance` hold (an owner-stopped
     row takes the hold without a new transition): the connector uploads and
     verifies the normal three-object stop artifact, and refuses owner starts
     from here on;
   - the artifact is copied server-side to
     `s3://<bucket>/<prefix>cutover/<host_id>/rollback/` (the product deletes
     the previous generation's objects on the workspace's next stop, so this
     copy is what keeps rollback possible) and its coordinates are saved to
     the state file;
   - the row is parked (the 409 guard covers user starts);
   - the data disk is transplanted onto the target (fresh gen-2 btrfs disk,
     `send | receive` of the home subvolume, quota qgroup), the slice is
     reserved at freshly picked free ports, booted, the latchkey software
     installed and the harvested `~/.latchkey` files and drop-ins written,
     the version's image tar published lazily and loaded, the container
     recreated from the harvested inspect (runsc, tmpfs, memory limits
     applied), keys replayed, the autostart installer run, the tmpfs
     secrets written and `latchkey-gateway` / `latchkey-tunnel` started
     (skipped when the origin's gateway was down: the desktop's next
     provisioning pass supplies the pair), and the health probe polled;
   - the row is re-leased at the target's address and ports, and the origin
     VM is destroyed (skipped by `--keep-origin-vm`, an early-drill safety
     net: the kept VM still carries the migrated row's instance name, so the
     orphan reap treats it as tracked and never collects it -- destroy it by
     hand on the origin box, e.g. `sudo -u limahost limactl delete -f <name>`,
     once the migration is judged good).
   Run several invocations with disjoint `--target-server-id` values to
   parallelize; the per-target-box and per-workspace locks refuse overlaps.
3. Verify (per migrated workspace, or a sample): the desktop opens it at the
   new address with no host-key prompt; `/home/user` intact; a package
   installed before the migration is present after env-converge;
   `supervisorctl status` all RUNNING/EXITED; stop/start from the UI works;
   `machines resize` + restart works; latchkey: `supervisorctl status` on
   the VM (not the container) shows `latchkey-gateway` and `latchkey-tunnel`
   RUNNING, `/root/.latchkey/credentials.json.enc` is present, and an agent
   can use a service granted before the migration without re-granting it and
   without an app restart (the report's per-workspace detail says how much
   latchkey state the migrate carried: "latchkey state replayed and the
   gateway restarted", "latchkey files replayed; the gateway starts at the
   desktop's next provisioning pass" when the origin's gateway was down, or
   "no latchkey state on the origin"; a 0.6.1+ desktop follows the move
   within one discovery cycle,
   while a <= 0.6.0 desktop keeps wiring the old VM for its desktop-forwarded
   routes -- permissions, the Minds API -- and needs an app restart, so
   migrate 0.6.1+ cohorts first). For the first drills, also open one from a
   deliberately held-back 0.5.x desktop client.

## When a migration stays FAILED

A running `migrate`, `rollback` or `repave` can be killed at any point
(Ctrl-C and SIGTERM both end it immediately; nothing is cleaned up on the
way out, which is what the state files and locks are for).

Re-run the same `migrate` invocation: it resumes from the state file (a
finished stop is not re-run; a transplanted disk is rescued from a half-built
slice; the reserve reclaims its own leftover dir). A `--source-server-id`
sweep re-selects the rows an earlier run parked off that box onto the same
target even though a parked row no longer sits on any box (its state record's
origin ties it to the sweep); a row parked onto a *different* target is
reported in the log and left for the invocation that owns it. If the health probe keeps
failing, inspect the slice on the target box (VM SSH with your operator
certificate -- `ssh -i ~/.mindsadmin/<tier>/ssh_id -p <reserved port> root@<box>`
-- at the reserved port) -- the row stays parked (users see the 409) until the probe
passes or you roll back.

## Rolling back

`minds-admin cutover rollback --yes-i-mean-<tier> --workspace <host_db_id>`
works from both the parked-mid-migration and the completed-migration states:
it parks the row back onto gen-1, destroys the gen-2 slice (and any kept
origin VM -- the product restore recreates the instance under the same name),
writes the saved artifact pointers back (re-stamping the harvested host
keys on a mid-migration rollback, since the restored VM serves them at fresh
ports) so the row is an ordinary finalized-stopped gen-1 row, admin-starts
it, and waits for `leased`. The
product's own gen-1 restore does all the work, onto whichever gen-1 box has
room -- so keep at least one gen-1 box per region until the rollback horizon
is declared closed. **Work done on gen-2 after the migration is lost**:
rollback is for migrations judged failed promptly, not a general gen-2 ->
gen-1 path (a workspace with real gen-2 work gets fixed forward).

## Repaving emptied boxes

Once a gen-1 box holds no pool rows (workspaces migrated off; `available`
rows destroyed) and no in-flight migration references it:
`minds-admin cutover repave --yes-i-mean-<tier> --server-id <box>`. There is
no default scope -- repaving reinstalls the box's OS. The box comes back
`ready` on gen-2 with its storage partition measured; bake it with
`minds-admin pool create --from-tag minds-v0.6.x` rows (the bake guard
refuses older tags on it).

## Signals to watch (promotion stays operator judgment)

- `SLICE_UNIT_OOM_KILLED` (must stay zero), box health, watchdog.
- Bugsink novelty attributable to gen-2 workspaces.
- Per-branch `host_lease_request` outcomes (an `update_required` rate is old
  clients still creating; `pool_exhausted` on a 0.6.x branch is gen-2
  capacity lagging the cohort) and the pool gauges per template branch.
- Migration and rollback counts in the state dir's reports.

## Endgame and cleanup

- Stragglers: an announced forced-migration sweep of the remaining gen-1
  workspaces (admin-started as needed), then the last gen-1 boxes repave.
- Phase 6 (delete the `cutover` group, the connector's parked-row guard
  (`_raise_if_workspace_is_migrating`), and the gen-1 code paths) requires **zero `box_generation = 1` rows
  in every tier's DB, in every status** -- a stopped gen-1 row's artifact is
  restorable only by gen-1 code -- and the rollback horizon closed. The
  `max_box_generation` lease field outlives phase 6 (it gates on client age).
- Add to `next_deploy.md` when the time comes: delete
  `s3://<bucket>/<prefix>cutover/` (the rollback copies and image tars) after
  a date well past the last migration.
