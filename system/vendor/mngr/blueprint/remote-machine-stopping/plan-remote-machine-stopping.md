# Plan: imbue_cloud workspace stop/start (remote machine stopping)

## Overview

- Today an imbue_cloud workspace occupies a bare-metal slice slot whether or not it is running (~$10/user/month); `mngr stop` only stops the docker container inside the still-running lima VM.
- Change `mngr stop`/`mngr start` for imbue_cloud to stop/start the whole slice VM: stop halts the VM and uploads its disks to OVH Object Storage, freeing the slot; start downloads them onto any same-region box with a free slot and boots.
- Proven by a live cross-box migration prototype: the artifact is 2 self-contained qcow2 files (`disk` ~12G, `datadisk` ~1.3G fresh) plus `lima.yaml` and small sidecars; lima discovers hand-placed instance dirs with no registration; stop takes ~4s, boot ~12s, the workspace container auto-starts via the existing reboot-resilience units, and per-host SSH host keys survive relocation.
- Measured transfer rates from boxes: download ~1 GB/s; upload currently throttled by OVH ingest to 6-25 MB/s (parallel ops ticket; nothing blocks on it).
- The `pool_hosts` row is the stable identity across the whole lifecycle (`host_db_id` and `host_id` never change); only its status and placement columns change.
- Transitions are driven by a spawned per-transition Modal supervisor (0.25 CPU / 512MB, 2h timeout) that launches a dumb idempotent box-side script over SSH and polls its status file; an hourly watchdog cron re-spawns supervisors for in-flight rows with stale heartbeats, retrying indefinitely.
- Unified vocabulary: stop/start/stopped/starting in code, statuses, and UI. The transfer processes are "upload"/"download" internally, surfaced to users at most as "fetching". No freeze/thaw/chill terms anywhere.
- v1 uploads whole files (zstd + age); content-addressed chunk dedupe (the ~11G of DWT docker-image bytes shared across same-tag workspaces) is a planned phase 2 that cuts both directions to ~1-3GB of unique bytes.

## Expected behavior

- `mngr stop <agent>` on an imbue_cloud workspace: gracefully stops the container, stops the VM, and returns within seconds. The workspace immediately shows STOPPED everywhere; the upload continues invisibly server-side.
- The slot is freed at max(upload verified, local-retention window — default 1h, per-env setting). A restart inside that window cancels finalization and resumes near-instantly on the same box.
- `mngr start <agent>` on a stopped workspace: asynchronously reserves a slot on a random same-region box with capacity, downloads and verifies the artifact, boots the VM, restarts the container sshd, and returns fresh connection coordinates. The client re-resolves address/ports and re-pins the (unchanged) host keys. Total wall clock ~1-2 min at current rates.
- minds desktop: imbue_cloud workspaces gain the Start/Stop button; opening a stopped workspace auto-starts it behind a "Waking up..." screen. Start failures (no capacity, corrupt artifact, boot failure) show an error with a Retry affordance; the row returns to `stopped` and the artifact is untouched. No automatic retry.
- Quotas: existing `max_remote_workspaces` caps *running* workspaces; new larger `max_total_workspaces` (explorer 10 / ally 50) caps running + stopped. Stop always succeeds. Create checks both; start checks running.
- Stopped workspaces still hold their lease identity: `GET /workspaces` (new endpoint) lists all lifecycle states with a `status` field; `GET /hosts` stays leased-only and is deprecated (removed once clients transition).
- Destroying a stopped workspace deletes its storage objects and row immediately; the restic backup's 30-day retention is the safety net. Re-stop keeps only the latest artifact generation (upload, verify, then delete the previous).
- Same-region placement only; "no capacity right now, try again later" is an acceptable start error (fleet stays over-provisioned).
- Old clients keep old container-only stop semantics until they update; no server-side forcing.
- Operators: `mngr imbue_cloud admin` gains an abandon (mark-crashed) subcommand for rows stuck on permanently dead boxes; recovery is backup restore. A box reboot never resurrects a stopping VM (the autostart unit skips marked instances).
- Encryption: per-stop random DEK encrypts both disks (streamed zstd | age); the DEK is wrapped by a per-env KEK (Vault -> Modal secret) and stored on the row; ciphertext sha256s live in the DB manifest and are verified before boot. Boxes never see the KEK.
- Auto-stop-on-idle is out of scope but designed-for: the in-VM idle watcher can power off the guest, and the watchdog cron treats a powered-off-but-leased VM as a stop request (future).

## Implementation plan

### remote_service_connector (apps/remote_service_connector)

- `migrations/00X_workspace_stop_start.sql`: extend `pool_hosts` — new status values (`stopping`, `stopped`, `starting`, `crashed`); make placement columns (`vps_address`, `ssh_port`, `container_ssh_port`, `bare_metal_server_id`, `lima_instance_name` stays) nullable where needed; add `stopped_at`, `artifact_manifest jsonb` (object keys, sha256s, sizes, lima version, generation), `wrapped_dek`, `transition_heartbeat_at`, `transition_error`.
- `imbue/remote_service_connector/workspaces.py` (new `APIRouter`):
  - `GET /workspaces` and `GET /workspaces/{host_db_id}`: all lifecycle states + `status`, coordinates when placed, `transition_error` when failed.
  - `POST /workspaces/{host_db_id}/stop`: owner-auth, CAS `leased -> stopping`, spawn the supervisor, 202.
  - `POST /workspaces/{host_db_id}/start`: owner-auth, quota check (`max_remote_workspaces`), CAS `stopped -> starting` (or fast path: cancel a still-local `stopping` row and relaunch in place), spawn the supervisor, 202; client polls the GET.
- `imbue/remote_service_connector/stop_start.py` (new): the supervisor + helpers —
  - render the box-side upload/download bash (streamed inline like the teardown commands; `zstd | age | s5cmd` pipelines, status file writes, idempotent + resumable, launched detached via `systemd-run --user`);
  - stop flow: `limactl stop`, write the autostart-skip marker, launch upload, poll status file, verify sha256s, delete previous generation, then at max(verified, retention window) delete VM+disk, null placement, CAS -> `stopped`;
  - start flow: pick a random same-region box with a free slot, reserve slot+ports under the box flock (reuse/adapt the reserve-script logic from `mngr_imbue_cloud.slices.lima_slice` — duplicated here, the connector cannot import the monorepo), download + verify + decrypt into place, rewrite `hostPort`s in lima.yaml, `limactl start`, restore container sshd reachability, rewrite placement columns, CAS -> `leased`;
  - DEK generation + KEK wrap/unwrap (AEAD via `cryptography`), per-box one-download-at-a-time lock, heartbeat writes each poll tick.
- `app.py`: register the supervisor Modal function (0.25 CPU / 512MB, 2h timeout) and the hourly watchdog cron (re-spawns supervisors for in-flight rows with stale heartbeats); attach the new `storage-<env>` Modal secret (S3 endpoint/creds, KEK, bucket name, retention window).
- `entitlements.py` + `hosts.py`: add `max_total_workspaces` (10/50) to plan definitions; lease/create path checks running AND total; running count = rows in `leased`/`stopping`/`starting`; total = those + `stopped`.
- `hosts.py`: mark `GET /hosts` deprecated (CLEANUP comment: remove once released clients use `/workspaces`); release path handles a `stopped` row (delete objects + row, no box SSH).
- Admin endpoint for abandon (admin-key authenticated): CAS in-flight/`stopped` row -> `crashed`, record reason.

### mngr_imbue_cloud (libs/mngr_imbue_cloud)

- `connector/client.py`: methods for `/workspaces` list/get/stop/start + abandon.
- `providers/instance.py`:
  - `stop_host`: graceful docker stop (as today) then `POST .../stop`; return once 202 confirmed.
  - `start_host`: `POST .../start`, poll `GET /workspaces/{id}` until `leased` (or error), refresh persisted lease meta, re-pin host keys under the new address/ports, wait for VM+container sshd, docker start + relaunch container sshd (existing logic).
  - discovery: consume `/workspaces` (fall back to `/hosts` against old connectors); stopped/stopping rows surface as offline hosts with state STOPPED.
- `cli/admin.py` (or `cli/hosts.py`): `admin workspaces abandon <host-db-id>` subcommand.
- `slices/bare_metal_prep.py`: prep installs pinned `age` + `s5cmd`; `mngr-slices-autostart.service` skips instances carrying the stop marker.
- `providers/instance.py` destroy path: a stopped workspace destroy goes through the connector release (which deletes objects + row) and local state cleanup only.

### minds (apps/minds)

- `envs/providers/ovh_s3.py` (new): per-dev-env S3 user + bucket provisioning via the OVH API at `minds env deploy` (like Neon); teardown at `minds env destroy`; bucket `mngr-workspaces-<env>`, region matched to the tier's boxes.
- `cli/env.py` + `config/envs/*/deploy.toml` + `.minds/template/storage.sh`: KEK + S3 creds flow (dev auto-generated, staging/production via Vault runbook); push into the `storage-<env>` Modal secret; retention-window setting.
- `desktop_client/`: Start/Stop for imbue_cloud workspaces (reuse the local-workspace button plumbing + confirmation), "Waking up..." screen driven by the start poll, auto-start on opening a stopped workspace, error + Retry surface.
- Docs: `docs/host-pool-setup.md`, `docs/environments.md`, `docs/workspace/glossary.md`, security-boundaries note (operator-decryptable artifacts), staging/production storage runbook.

### mngr core (libs/mngr)

- No structural changes expected: `stop`/`start` already dispatch to the provider. Verify stop/start command docs mention the imbue_cloud behavior and that start timeouts accommodate the ~1-2 min path.

### default-workspace-template

- No changes required for v1 (the volume-gated autostart units already bring the workspace up on boot).

## Implementation phases

1. **Storage infra + stop path.** Migration; `minds env deploy` S3/KEK provisioning; box prep additions (age, s5cmd, autostart marker); `GET /workspaces`; `POST .../stop`; supervisor upload flow; watchdog cron. Milestone: curl-driven stop on a dev env — VM gone after retention, objects + manifest in the bucket, slot freed, restart-in-window works.
2. **Start path.** `POST .../start`; supervisor download/boot flow with port re-reservation and random box selection; failure CAS + `transition_error`; re-stop generation cleanup; destroy/release of stopped rows. Milestone: curl-driven full stop/start cycle across boxes on dev.
3. **mngr client.** Provider stop_host/start_host rewire; `/workspaces` client + discovery; coordinate refresh + key re-pinning; admin abandon subcommand. Milestone: `mngr stop` / `mngr start` end-to-end on dev (the CLI milestone).
4. **Quotas + minds UI + hardening.** `max_total_workspaces` entitlement + checks; minds Start/Stop/"Waking up..."/auto-start-on-open + error/Retry; deployment_tests scenario; docs + runbooks; `/hosts` deprecation notes; file the OVH upload-throttle ticket (full URL reported). Rollout: dev -> staging -> production (boxes re-prepped per the usual runbook), no feature flag.

## Testing strategy

- Unit tests (connector): status CAS transitions (legal/illegal/idempotent), DEK wrap/unwrap round-trip, box-script rendering snapshots, manifest verification, quota counting per status, box selection (region filter + capacity).
- Unit tests (mngr_imbue_cloud): client methods against a mock connector; stop_host/start_host flows with a mock client + mock outer host; discovery mapping of stopped rows to offline STOPPED hosts.
- Integration tests: workspaces router request/response shapes with the existing connector test fixtures; `/hosts` remains leased-only.
- deployment_tests (operator-invoked, dev env, real box + bucket): create (or adopt a baked slice) -> stop -> assert slot freed + objects exist + `mngr list` shows STOPPED -> start -> assert markers/container/ssh on the new box -> re-stop (previous generation deleted) -> destroy (objects gone).
- Edge cases to cover: restart during `stopping` (fast path, upload canceled); double stop/start (idempotent 202); start with no same-region capacity (clear error, row stays `stopped`); sha256 mismatch on download (error, artifact untouched); orphaned supervisor re-driven by the cron; box reboot mid-upload (autostart skips the marked VM, upload resumes); quota boundaries at create/start; old-client stop against new connector (unchanged container-only behavior).
- Manual verification before completion: the full cycle exercised on the dev env exactly as a user would (minds UI stop, close, reopen, waking screen), per the repo's manual-verification rule.

## Open questions

- How should `crashed` rows render in minds (v1 could show a generic error state with a link to backup restore)?
- Does the transient `hostPort` bind race observed once at start need a bounded retry in the supervisor, or is lima's own retry sufficient?
- KEK rotation runbook for staging/production: required at launch, or documented as a follow-up (rotation only re-wraps DEKs, no data re-encryption)?
- Should the box-side upload throttle itself (nice) when the box is running a bake, or is contention acceptable at v1 scale?
- Exact `/hosts` removal condition: after the desktop client's next mandatory-update cycle, or after telemetry shows no `/hosts` callers?
