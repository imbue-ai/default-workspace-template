# Phase 5.5 -- incremental gen-2 rollout: migrate, rollback, cohorting, lease guard

Status: decided 2026-09-05 (Josh + the phase-5.5 planning session); implemented on this branch; the dev drill (the exit criterion) ran on dev-josh-2 2026-09-06 and PASSED -- every leg including the held-back-0.5.x-client one (results and the three bugs it surfaced are on PR #846).
This document refines the rollout half of
[`plan-slice-fleet-cutover.md`](./plan-slice-fleet-cutover.md) and is authoritative
wherever the two differ: it **replaces the per-tier flag-day model** (Phase 5's "announce,
preflight, window, report" and the phase-4 `drain`/`restore` stages) with an incremental,
per-workspace migration. [`phase-4-cutover-tooling.md`](./phase-4-cutover-tooling.md)
remains authoritative for the machinery this phase reuses (transplant, harvest-and-replay,
image tars, state dir, disk accounting); its drain/restore *stage drivers* are deleted here.

Implementation lands on branch `new-fleet-phase-5.5`, stacked on `new-fleet-phase-4-impl` (PR #737),
so the whole stack merges to `main` already able to run these migrations.

## Overview

- **Incremental instead of flag-day.** Workspaces move to gen-2 one at a time (or per user,
  or per source box), each through its own stop + restore; gen-1 boxes are repaved to gen-2
  only once they are empty. No tier-wide windows, no simultaneous downtime.
- **Cohorting rides the release channels.** 0.5.x releases are gen-1, 0.6.x+ releases are
  gen-2; a bake-time tag guard keeps the two generations' pools disjoint, so the exact-tag
  lease match routes each client version to its generation. Promoting 0.6.x through
  alpha -> beta -> stable is the rollout throttle.
- **Migrate reuses the product's own gen-1 stop.** The migration's upload is the connector's
  verified three-object stop artifact (boot disk + data disk + meta), not a bespoke drain --
  which makes rollback the product's own gen-1 restore, with zero new gen-1-side transfer
  or restore code.
- **Old clients can never accidentally land on gen-2.** A `max_box_generation` capability
  field on the lease request (absent = 1, and pre-0.6 clients cannot send it) confines them
  to gen-1 rows on both the fast and slow paths; once gen-1 stock retires, their creates
  fail with a clear update-required error.
- **The flag-day stage code is deleted now, not in phase 6.** `cutover drain` and
  `cutover restore` (and their gen-1 upload script, fixed-ports reserve variant, and
  tier-refusal fit logic) go away in this phase; one migration path, fully drilled.

## Expected behavior

User-visible:

- A migrated workspace sees a stop, then (minutes to tens of minutes later, dominated by
  its data size) a start at a **new address and ports** on a gen-2 box. The client
  re-resolves coordinates automatically; harvested VM and container host keys mean no trust
  prompt. `/home/user`, the workspace version, apt records, and container identity are
  intact; afterwards the container runs under gVisor (same caveats as the cutover:
  ptrace/FUSE/io_uring gone, metadata-heavy filesystem ops slower).
- While a workspace is mid-migration, Start answers the existing 409
  `workspace_migrating`; each workspace's downtime is its own stop + restore, nothing else.
- A rolled-back workspace comes back on gen-1 (on whatever gen-1 box the product restore
  picks) at its pre-migration state: **work done after the migration is lost by policy** --
  rollback is for migrations judged failed promptly, not a general gen-2 -> gen-1 path.
- Old (<= 0.5.x) desktop clients keep working throughout: their existing gen-1 workspaces
  are untouched until migrated; a migrated workspace still opens, runs, and stop/starts
  from an old client; their *new* creates only ever lease gen-1 rows, and once a tier's
  gen-1 stock is retired those creates fail with an "update the app" error instead of
  silently landing an unsandboxed container on a gen-2 box.
- New (0.6.x+) clients' creates lease gen-2 rows via the tag match; if the gen-2 pool is
  exhausted their slow path may still rebuild on a gen-1 row (capability field permits
  both) -- safe, just not a gen-2 workspace.

Operator-visible:

- `minds-admin cutover {preflight, migrate, rollback, repave}` -- `drain` and `restore`
  are gone. All env-aware, state-dir-backed (`~/.minds-<env>/cutover/`), `--dry-run` on
  migrate and repave, `--yes-i-mean-<tier>` gates kept.
- `cutover migrate --yes-i-mean-<tier> --target-server-id <gen2-box>` plus selectors:
  repeatable `--workspace <host_db_id>`, `--user <email>` (all their leased/stopped
  workspaces), `--source-server-id <gen1-box>` (everything on the box). One invocation
  processes its workspaces **sequentially onto the one target box**; parallelism is
  multiple invocations with disjoint targets, enforced by per-target-box and per-workspace
  locks in the state dir. Per workspace: verify target budgets; live-harvest keys,
  `docker inspect`, and `git describe`; admin product stop (auto-starting a stopped row
  first and waiting for `leased`); save the row's artifact manifest + wrapped DEK to the
  state file; park the row; transplant the artifact's
  data disk into a fresh gen-2 disk; reserve/boot/recreate the container on the target box
  (phase-4 restore machinery, free ports); health-probe; CAS to `leased` gen-2 at the new
  coordinates, stamping the harvested host keys onto the row (the recorded bake-time keys
  can predate a client-side adopt rotation, and record-sync clients pin the row's keys for
  the new address); then delete the origin lima VM and disk (`--keep-origin-vm` skips this for
  early drills, and the report notes the kept VM must be finalized by hand before its box
  is baked on). Stops on first failure; re-runs skip completed workspaces and never touch
  successes. The image tar for the workspace's version is published lazily when missing
  (`--publish-image-tars` remains as an explicit pre-warm).
- A failed health probe leaves the workspace parked with its half-built slice inspectable;
  the operator chooses `migrate` re-run (retry) or `rollback`.
- `cutover rollback --yes-i-mean-<tier> --workspace <host_db_id>`: destroys the gen-2
  slice, writes the saved artifact manifest + wrapped DEK back onto the row, flips it to
  finalized-stopped gen-1 (NULL placement), then calls the admin start and waits for
  `leased` -- the product's own gen-1 restore does all the work. Works from both the
  parked-mid-migration and the completed-migration states.
- `cutover repave` refuses a box holding any pool rows (destroy `available` rows via
  `pool destroy` first); otherwise as phase 4 (gen-2 flip, reinstall, prep, measured
  `disk_gb`). Beachhead per region: production orders a fresh box; dev/staging repave a
  box with zero leased rows.
- `cutover preflight` becomes inventory + eligibility only (version floor, health,
  `git describe`, per-workspace sizes, distinct-version set); the tier-refusal fit
  computation is deleted (fit is now checked per migrate against the target box).
- Bake guard: a gen-2 box bakes only `minds-v0.6+` tags, a gen-1 box only `minds-v*` tags
  below 0.6; non-tag refs (dev branches) are exempt, and the image-tar seed bake is exempt
  (its row never leases; it is destroyed once the tar exists).
- The runbook lists the signals to watch (SLICE_UNIT_OOM_KILLED, Bugsink novelty,
  per-branch lease-outcome and pool-gauge metrics, migration/rollback counts) but channel
  promotion stays operator judgment.

System behavior:

- `POST /hosts/lease` gains an optional `max_box_generation` field: rows with
  `box_generation` above it are excluded; absent defaults to 1. When the cap (not
  capacity) is what emptied the candidate set -- the tier has no gen-1 rows left in any
  status -- the refusal carries an update-required ("update the app") message instead of
  the generic no-capacity error. 0.6.x+ clients send 2 on
  every lease (fast and slow -- the slow path's relaxed attributes drop the tag, so this
  field is the only thing keeping old slow-path leases off gen-2). `/hosts/claim` derives
  it from whether its pinned fallback tag is >= 0.6. `# CLEANUP:` remove when the pre-0.6
  client population is dead (this outlives phase 6).
- The mixed fleet works as today until the end: gen-1 stop/start, watchdog, and reconcile
  paths are untouched; migration uses only existing row states (`stopping`/`stopped`,
  parked, `leased`) so nothing new is visible to the supervisors.
- Phase 6's gate changes from "after production's window" to: **zero `box_generation = 1`
  rows in every tier's DB, in every status** (a stopped gen-1 row phase 6 would strand is
  the trap), plus a declared-closed rollback horizon (keep at least one gen-1 box per
  region until then). The endgame is a mini flag-day for stragglers (announced
  forced-migration of remaining gen-1 workspaces, admin-started as needed).

## Changes

Connector (`apps/remote_service_connector`):

- Lease selection honors `max_box_generation` (default 1), answering the cap-emptied
  no-gen-1-stock case with the update-required error; claim derives its value from
  the pinned fallback tag. No other connector changes -- the `workspace_migrating` guard,
  admin stop, and admin start from phase 4 are reused as-is.

Plugin (`libs/mngr_imbue_cloud`):

- The lease client sends `max_box_generation = 2` on every lease request.

Operator tooling (`apps/minds_admin`):

- New `cutover migrate` and `cutover rollback` drivers (selectors, locks, budgets,
  auto-start, harvest, product-stop orchestration, saved-artifact bookkeeping, transplant +
  restore reuse, probe, CAS, lazy image-tar publish).
- Delete the `drain` and `restore` stage drivers, the gen-1 upload script renderer, the
  fixed-ports reserve variant, and preflight's tier-refusal fit logic; simplify `repave`
  to empty-box-only.
- Bake-time tag/generation guard in the bake path (with the branch-ref and seed-bake
  exemptions).
- State dir: per-workspace saved `artifact_manifest` + `wrapped_dek`, migration stage enum,
  per-target-box and per-workspace lock files.

Docs:

- Rewrite `plan-slice-fleet-cutover.md` Phase 5 as the incremental sequence (beachhead,
  channel promotions, migrate waves, rolling repaves, straggler endgame) and amend the
  phase-6 gate; rewrite `gen2-cutover.md` around the new stage set (migrate/rollback
  runbook, signals list, rollback-horizon and last-gen-1-box rules); note in
  `next_deploy.md` that gen-2 bakes are 0.6.x+ once this merges.

Testing (unit + drills only; no CI release test -- the tooling dies in phase 6):

- Unit: the capability filter (absent/1/2 against mixed rows), claim derivation, the bake
  guard (tags, branches, seed exemption), migrate selector resolution and lock behavior,
  the saved-manifest round-trip and rollback flip SQL against the fake store, budget
  refusal, report rendering.
- Exit criterion drill on dev: bake 2-3 gen-1 workspaces across two source boxes (one
  stopped), migrate them onto the gen-2 box (one invocation per target), verify
  data/apt/stop-start/resize on gen-2, kill-and-rerun the driver mid-migrate, roll one
  back and verify it returns on gen-1 via the auto-start, exercise a held-back 0.5.x
  client against a migrated workspace (open, use, stop/start), and confirm an
  old-client-shaped lease (no capability field) never receives a gen-2 row.
