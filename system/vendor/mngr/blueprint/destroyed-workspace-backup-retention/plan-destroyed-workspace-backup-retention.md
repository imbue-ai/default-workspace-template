# Destroyed-workspace backup retention and reaping

## Overview

- Destroyed workspaces currently leak their backups forever: R2 buckets, connector workspace records, and local `restic.env` files are never cleaned up, so they accumulate until the per-account bucket quota (20) is exhausted and new workspaces silently fail backup provisioning (the `staging-remote-2` bug).
- Replace "backups live forever" with a uniform 30-day retention policy after destroy, enforced by two idempotent reapers: a client-side reaper (all provider types and backends) and a server-side reaper on the connector (imbue_cloud backstop that works with no client running).
- Model everything on mngr's existing destroy-vs-delete pattern: destroy tombstones with a timestamp; periodic reapers permanently delete tombstones older than the retention window. minds' managed mngr profiles also get `default_destroyed_host_persisted_seconds = 30 days` for consistency (mngr's built-in 7-day default is unchanged).
- Fix the quota bug at its root with client-side quota-pressure eviction: when backup provisioning hits a bucket-count or storage quota, it force-destroys the oldest reapable backup (something that would age out anyway) and retries.
- Give users visibility and control via a "Recently destroyed workspaces" page: download backups before they age out, delete them early to free quota, see the countdown.

## Expected behavior

### Retention policy

- Destroying a workspace keeps its backup (bucket + record + local env) for 30 days, then reapers delete all three permanently.
- The 30-day value is a fixed constant served by a new public connector endpoint `GET /policies/destroyed-workspace-backups`, with a baked-in 30-day client fallback (per-plan retention is an explicit future option, not built now).
- The policy is uniform — no opt-out. Downloading within the window is the escape hatch.
- Any record tombstoned `destroyed` for longer than 30 days is reapable regardless of how it was tombstoned (explicit destroy or absence detection) — the window itself is the safety margin for local workspaces.
- BYO backup backends (user's own S3/API key) are never reaped: only the local env and the record are cleaned up; repository data stays until the user removes it.
- Pre-existing tombstones and orphan buckets/envs start their 30-day clock at rollout (grace period), so users get a month to notice the new page and download before the first destructive sweep. The existing staging/dev backlog just ages out — no one-time manual cleanup.

### Client-side reaper (all workspace types)

- Runs inside the existing sync-scheduler reconcile loop, but on its own cadence: every 30 minutes plus one pass ~2 minutes after startup; the reconcile tick itself stays at 60s.
- Bucket deletions happen on a background thread created via the concurrency group, guarded by a lock so only one reap run is ever in flight.
- Two scans per pass: tombstoned records past the window, and local `backup_envs/*.env` referenced by no record at all (orphans, aged by first-seen stamp).
- Caps at ~5 reaps per pass so the post-grace backlog drains over a few hours without stalling anything.
- Deletion order is strict: bucket first (empty client-side, then destroy); on failure the record and env stay untouched and the pass retries later. Record, then local env, are deleted only after the bucket is gone. The local `restic.env` is deleted outright, not archived.
- Skips any bucket with an in-flight export (download) this pass.
- Degrades quietly against a not-yet-updated connector: bucket-deletion steps are skipped (debug log) and retried next tick.

### Server-side reaper (imbue_cloud backstop)

- A new periodic connector task beside the existing storage sweep (hourly Modal cron) with two idempotent rules: destroyed workspace records past the window (delete bucket, then record), and workspace-backup buckets referenced by no record at all, aged past the window since first seen orphaned.
- Orphan age comes from first-seen stamping: a small connector table records when each workspace-backup bucket was first observed orphaned; the 30-day clock runs from that stamp (which also yields the rollout grace for free).
- Reapers identify workspace-backup buckets by the `host-<hex>` short-name convention; `bucket create` reserves the `host-` short-name prefix so user-created buckets can never collide (none collide today). Generic user buckets are never touched by reapers.
- Its bucket emptying is bounded per cron pass and resumable — a partially-emptied bucket continues next pass; the bucket+record deletion lands on the pass that finishes (never a long-running Modal request).
- An `/admin/sweep/...`-style on-demand trigger runs the reaper immediately, with `?dry_run=1` (returns candidates: id, kind record/orphan, destroyed/orphaned-at, age — no sizes) and an admin-only window override (e.g. `?window_seconds=0`) so the deployment test or an operator can reap fresh tombstones.

### Bucket destruction path (shared)

- No new server-side force/emptying endpoint. The existing owner destroy endpoint keeps refuse-if-non-empty and gains one interlock: it refuses to destroy a `host-` bucket referenced by an ACTIVE workspace record — tombstone-first is enforced server-side, so a live workspace's backups can never be deleted.
- Emptying is client-side: the imbue_cloud CLI owns empty+destroy end-to-end as a documented `mngr imbue_cloud bucket destroy --force` (batched S3 deletes reusing the existing `r2_cleanup` boto3 logic, then the ordinary destroy call; interactive confirmation unless `-y`). boto3 becomes an mngr_imbue_cloud dependency; minds' env tooling imports the moved helper.
- `--force` works on any bucket the caller owns; the ACTIVE-record interlock applies only to `host-` buckets.
- When keys are quota-downgraded to read-only, emptying reuses the existing cleanup-grant machinery (same flow as trim) so deletion always has write access.
- Every minds deletion path (reaper, eviction, "Delete backup now") invokes the CLI — a single implementation.

### Quota-pressure eviction (the bug fix)

- When backup provisioning hits a quota limit, the client evicts: candidates are identical to the reap set (tombstoned records + orphans for that account), ordered oldest-first (past-window first, then within-window early).
- Both quota types (bucket count and storage bytes) use the same policy; live workspaces' data is never touched automatically (trim stays a manual action).
- Eviction loops oldest-first until provisioning succeeds or candidates run out; on the first force-destroy failure it aborts and provisioning fails with today's notification. CLI `bucket create` still fails hard on quota.
- Eviction is silent — no notification; the recently-destroyed page reflecting the removal is sufficient.
- The connector's quota rejections gain machine-readable error codes (`quota_exceeded_buckets` / `quota_exceeded_storage`) passed through the CLI's JSON output; eviction keys off the code, not message text.
- Early deletion (eviction or "Delete backup now") is atomic in effect: bucket, then record, then env — the row vanishes and nothing is left for the day-30 sweep.
- No new heal UI: the existing workspace-settings "enable backups / change where backups go" action is the heal path for workspaces whose create-time provisioning failed (it now self-heals quota via eviction).

### Recently destroyed workspaces page

- A full local page on the chrome surface (like `/accounts`) at `/workspaces/destroyed`, with Back navigation, linked as "Recently destroyed workspaces" from the Landing page's bottom-left launcher area.
- All tombstoned records within the window appear as rows, sorted newest-destroyed first; rows without backups simply lack the download/delete buttons.
- Rows from all signed-in accounts merge into one list with an account label per row; orphan buckets/envs with no record appear as "unknown workspace (agent-…)" rows labeled "this device".
- Each row shows a days-until-reap countdown, a download (export-as-zip, reusing the existing export machinery), and "Delete backup now".
- "Delete backup now" asks for confirmation via a native dialog naming the workspace and stating the backup is gone forever.
- Downloads work from any signed-in device that can decrypt the record's synced encrypted secrets (reconstructing the restic env); rows needing the sync master password show a locked state up front ("unlock to download") reusing the existing unlock affordance.
- For an orphan row whose owning account isn't signed in, download works (local env) but delete-now is disabled with a "sign in as <account> to delete" hint.
- When an account is over either quota, the Plan & Usage section (modal and full accounts page — shared component) adds a "review destroyed workspace backups" link beside the trim CTA.

### Copy, docs, observability

- New-policy copy on: the destroy-workspace confirmation dialog in workspace settings ("backups are kept 30 days, then deleted" — minimal, no page link), the destroyed page header, the workspace-settings backups section, and user docs.
- Docs land in three places: a minds backups-lifecycle section (retention, reaping, eviction, the page), the mngr imbue_cloud CLI docs (bucket commands + reserved prefix), and the connector docs (reaper, orphan-stamp table). No separate release/ops runbook note.
- Both reapers log; the server reaper exposes counters on the existing admin/sweep surface; the client records reap/eviction events into events.jsonl under the `backup_reaper` source.
- `bucket list` is unchanged (no pending-reap annotation) — dry-run on the admin trigger is the ops view.

## Changes

- Connector: add `destroyed_at` to workspace records (migration stamps existing destroyed rows at rollout for the grace period); add the orphan first-seen-stamp table; add the hourly reaper cron + admin trigger with dry-run and window override; add the ACTIVE-record interlock to the owner destroy endpoint; reserve the `host-` short-name prefix in bucket create; add machine-readable quota error codes; add `GET /policies/destroyed-workspace-backups`; extend admin/sweep counters.
- imbue_cloud CLI (mngr_imbue_cloud): move the boto3 batch-delete emptying logic in from minds' env tooling (minds imports it back); add documented `bucket destroy --force` (empty client-side + destroy, confirmation unless `-y`); pass quota error codes through JSON output.
- minds client: stamp `destroyed_at` on tombstone; add the reaper step to the sync scheduler (own 30-min cadence, startup pass, concurrency-group background thread, single-flight lock, ~5-per-pass cap, export-aware skip, strict bucket→record→env order, orphan-env scan with first-seen stamping); add quota-pressure eviction to backup provisioning keyed off the new error codes; fetch/cache the retention constant with a 30-day fallback; inject `default_destroyed_host_persisted_seconds = 30 days` into managed mngr settings; record `backup_reaper` events.
- minds UI: the `/workspaces/destroyed` page (rows, countdown, download with locked-state handling, confirm-dialog delete-now, account labels, orphan rows) + Landing link; destroy-confirmation and backups-section copy; the Plan & Usage over-quota link.
- Docs: minds backups lifecycle, imbue_cloud CLI bucket commands, connector reaper/table docs.
- Tests: unit/integration across all pieces (fake connector covers eviction end-to-end); one deployment test driving tombstone → admin trigger (window override) → bucket + record gone, using the tier admin key from Vault.
