# Workspace stop/start (imbue_cloud)

An imbue_cloud workspace can now be stopped without holding its bare-metal
slot: `mngr stop` halts the slice's lima VM, uploads its disks (encrypted)
to the tier's OVH Object Storage bucket, and lands the row on `stopped` the
moment the upload verifies; the halted local VM (and its slot) is kept
through the local-retention window for a fast restart in place, then reaped
by the supervisor's retention-finalize phase. `mngr start` restores the
workspace -- near-instantly on its origin box within the window, or by
downloading onto any same-region box with a free slot after it. The minds
desktop shows the Start/Stop control for imbue_cloud workspaces through the
same gate as local ones.

The pool_hosts row is the lease/machine record; `host_db_id` and `host_id`
identify the machine (the slice VM keeps its host id across stop/start --
suspending and resuming a machine does not mint a new one). The workspace's
own durable identity is its workspace id (the pre-provisioned services agent
id on the row). Lifecycle:

```
running (leased) -> stopping -> stopped -> starting -> running
```

- `stopping`: VM halted, upload in flight (the user sees STOPPING); a start
  is refused with 409 until the upload verifies -- transitions only begin
  from the stable states, so stop and start supervisors never coexist.
- `stopped`: artifact verified in the bucket. The halted local VM (and the
  slot, via the row's box link) is kept through the local-retention window
  so a start within it restarts in place; the retention finalize then
  deletes the VM and clears the placement, freeing the slot.
- `starting`: a connector supervisor is restoring/booting it; a failed
  start always lands back on `stopped` with the error recorded.
- `crashed`: operator-abandoned (`minds-admin workspaces abandon`);
  the user recovers by restoring the workspace's backup.

## Moving parts

- **Connector** (`apps/remote_service_connector`): `GET /workspaces` (the
  full-lifecycle listing; `GET /hosts` stays leased-only and is deprecated),
  `POST /workspaces/{id}/stop|start` (async, 202 + poll), a per-transition
  Modal supervisor (0.25 CPU / 512MB) that drives the box-side scripts and
  finalizes DB state, and an hourly watchdog cron that *takes over* rows
  whose heartbeat went stale under a fresh `transition_id` fencing token
  (every supervisor write is guarded on it, so a superseded driver's writes
  hit zero rows). Re-drives back off exponentially in
  `transition_failure_count` and escalate to ops (error-level log) once a
  transition has clearly stopped converging; retries continue indefinitely,
  and abandoning a row is always a manual operator action.
- **Boxes**: prep installs pinned `age` + `s5cmd` plus apt `zstd`; transfers
  run as detached scripts (`zstd | age | s5cmd`) reporting through a status
  file. The
  boot-time slice autostart skips VMs carrying the stop-requested marker so
  a box reboot never resurrects a half-uploaded VM.
- **Artifact** (a machine image, not a workspace backup -- the restic
  backups remain the substrate-independent safety net): the slice's
  self-contained qcow2 `disk` + `datadisk`, plus a
  small metadata tar (`lima.yaml` and sidecars), keyed under
  `[<env>/]<host-id>/gen-<n>/`. Each object is encrypted to a per-stop age
  identity; the identity is wrapped by the tier KEK and stored on the row
  (committed *before* any byte uploads). Ciphertext sha256s live in the DB
  manifest and are verified before boot. Re-stops keep only the newest
  generation; destroy deletes the workspace's objects immediately (the
  restic backup's 30-day retention remains the safety net).
- **Quotas**: `max_remote_workspaces` caps *running* workspaces
  (leased/stopping/starting); the larger `max_total_workspaces` (free 5 /
  explorer 10 / ally 50) caps running + stopped. A just-stopped row counts as
  stopped even while its retained local VM still occupies a physical slot
  (pool capacity itself is enforced by real slot occupancy on the boxes).
  Stopping is always allowed; create checks both caps; start re-checks the
  running cap.

## Provisioning a tier (operator, once)

One bucket + one S3 user per tier. Dev envs share the dev tier's bucket --
the deploy stamps each env's `WORKSPACE_STORAGE_KEY_PREFIX=<env>/` override
automatically, so per-env artifacts (and their cleanup) stay disjoint.

1. Create an S3 user + credentials in the tier's OVH cloud project
   (`role=objectstore_operator`; `POST /cloud/project/<id>/user` then
   `POST .../user/<uid>/s3Credentials` via the OVH API, creds from the
   tier's `ovh` Vault entry).
2. Create the bucket (e.g. `mngr-workspaces-<tier>`) against the tier
   region's endpoint, e.g. `https://s3.us-east-va.io.cloud.ovh.us`
   (standard/`io` class; measured download from boxes ~1 GB/s).
3. Generate the KEK: `openssl rand -base64 32`.
4. Populate Vault from the template and deploy:

```bash
cp .minds/template/storage.sh /tmp/storage-<tier>.sh
$EDITOR /tmp/storage-<tier>.sh
uv run scripts/push_vault_from_file.py <tier> storage /tmp/storage-<tier>.sh
shred -u /tmp/storage-<tier>.sh
eval "$(uv run minds-admin env activate <tier>)"
uv run minds-admin env deploy --yes-i-mean-<tier>
```

`storage` is in every tier's `deploy.toml` services list, so the deploy
pushes it as the `storage-<env>` Modal secret the connector reads. A dev
env without the Vault entry populated still deploys (the deploy logs an
error and pushes a placeholder secret); the deployed connector then
cleanly refuses stop/start with a 503 until the entry is populated and
the env redeployed. Staging / production deploys hard-fail when the
entry is missing or misses template-declared keys -- push the entry
first (empty values are allowed to deliberately leave the feature
disabled). The dev tier's entry is populated (bucket
`mngr-workspaces-dev`).

A tier's `deploy.toml` may also declare a git-owned retention window
(`[storage] stop_retention_seconds`); the deploy stamps it as
`WORKSPACE_STOP_RETENTION_SECONDS` over the Vault entry, so git wins over
any stale Vault value. ci sets 60s and dev 300s so stop/start tests and
dev iteration see the retention finalize (slot freed, restore path
exercised) land in minutes; staging / production omit the block and keep
the connector's default hour-long instant-restart window.

## Operations

- `minds-admin workspaces abandon <host-db-id> --reason ...`
  marks a row on a permanently dead box `crashed` (retries stop; the user
  restores from backup; artifacts are reclaimed at release). Releasing a
  crashed row attempts the VM teardown best-effort: an unreachable box is
  logged and the release still completes, and if the box was actually alive
  the leftover VM surfaces in the box-reconcile sweep.
- A stuck transition is visible as a `stopping`/`starting` row (or a
  `stopped` row still holding its box link past the retention window) with
  a stale `transition_heartbeat_at` plus connector logs; the watchdog takes
  it over and re-drives it, backing off in `transition_failure_count` (the
  last failure is on `transition_error`) and logging at error level once it
  has failed many consecutive times.
- Restores on one box download one at a time under `~/.mngr-download.lock`
  (released before the VM boots). A restore queued behind another's
  download shows `STAGE=waiting-for-lock` in its box-side status file; the
  wait is bounded (`flock -w 300`), so a stuck lock fails the restore with
  `box download lock unavailable after 300s` on `transition_error` rather
  than stalling it. To find who holds the lock, walk `/proc/*/fd` for the
  lock path (not `/proc/locks`, which records the long-gone `flock(1)`
  pid); a `limactl hostagent` + `qemu` pair holding it is a leaked
  descriptor from a pre-fix restore, and unlinking the lock file (a new
  restore then locks a fresh inode) is the safe remediation.
- KEK rotation re-wraps the per-stop identities in the DB only -- objects
  are never re-encrypted.
- Known constraint: upload from boxes to OVH Object Storage is currently
  throttled server-side (~6-25 MB/s regardless of parallelism; download is
  ~1 GB/s), so slot reclaim after a stop takes tens of minutes. Tracked as
  a parallel ops investigation; content-addressed chunk dedupe (planned
  phase 2) cuts uploads to the workspace's unique bytes.

## End-to-end verification

`apps/minds/deployment_tests/test_workspace_stop_start.py` runs the full
lease -> stop -> (upload, slot freed) -> start -> SSH-verified restore ->
release cycle against a real env; it skips cleanly when the env has no
baked slice or no storage configured. Run it against a dev env with a
baked box:

```bash
just minds-test-services-against dev-<you> apps/minds/deployment_tests/test_workspace_stop_start.py
```

(The test is in the `minds_services` batch, so it needs the
services-against runner pointed at a deployed env;
`minds-test-deployment-only` runs only the `minds_deployment` batch and
would deselect it.)
