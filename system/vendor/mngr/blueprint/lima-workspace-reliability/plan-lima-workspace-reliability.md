# Plan: Lima workspace reliability and visible creations

Fixes the issues diagnosed in mngr-internal#121 (Lima workspaces stalling on readiness, duplicate VMs, orphaned VMs and host records, every-boot cloud-final failure, gc stranding VM definitions, no image-download retries), and adds a long-wanted UX feature: in-flight workspace creations are visible in the workspace list and their progress view is re-enterable.

## Overview

- The #121 investigation found six distinct defects. The dominant "stuck" cause is that a cold build-in-VM Lima create legitimately outlives the 300s readiness window; orphaned VMs come from unguarded host-name reuse and from creations whose `mngr create` subprocess died with the app, leaving no workspace association; hygiene defects (every-boot `cloud-final` failure from a missing btrfs filesystem label, `delete_host` stranding VM definitions, no download retry) compound the mess.
- Core strategy: enforce host-name uniqueness at the provider (with an early BUILDING reservation), make workspace identity durable (a `workspace-id` host label plus a local pending-creation record written before the create starts), and reconcile at startup — adopting completed-but-unassociated workspaces and destroying abandoned half-built hosts after a grace window.
- The pending-creation records double as the backing state for a new UX: every in-flight creation appears as a row in the workspace list (creating / interrupted / failed), clicking it returns to the log-streaming progress view, and interrupted/failed rows carry retry and dismiss actions — so users can start a create and go do something else.
- mngr gains host-targeted destroy (`mngr destroy @HOST[.PROVIDER]` / `host-<id>`), which the reconcile and row actions use, and which finally gives users a manual cleanup path for agent-less hosts.
- Packaging: three stacked PRs merged together — (1) mngr + mngr_lima fixes, (2) minds reliability, (3) creation-rows UX. The PRs reference issue #121; no separate issue comment.

## Expected behavior

### Creation readiness (Lima slow path)

- A Lima create that falls back to build-in-VM (no prebaked image resolved) waits up to 900s (hardcoded constant; other creates keep 300s) for the system interface before publishing the redirect.
- While a creation is in flight, the system-interface health tracker never drives that workspace to STUCK / the recovery page; suppression ends when the creation reaches DONE/FAILED or its readiness window expires, whichever comes first. Workspace start/restart behavior is unchanged.
- A cold build that outlives even 900s lands on the plugin's auto-refresh loading page (as today), which no longer flips to the recovery page mid-provisioning.

### Host-name uniqueness (all lima users, not just minds)

- `mngr create --new-host` on lima hard-fails with `HostNameConflictError` when a non-destroyed host record with the same name exists. FAILED records do not conflict, so retrying a name after a failed create works.
- The name is reserved at the very start of the create (a BUILDING host record written before `limactl start`), closing the multi-minute window where a concurrent same-name create could slip through. BUILDING records always conflict; discovery shows them as BUILDING.
- The conflict error names the conflicting host id and state; for BUILDING records it suggests `mngr destroy @<host-id>` to clear an abandoned create.
- minds-side, the create form's availability check, `start_creation`, and the `workspace-N` auto-namer all also consult live in-flight creations, so duplicates are rejected before mngr is even invoked and back-to-back auto-named creates just work.

### Durable association and startup reconcile (lima + docker)

- Every minds create writes a local pending-creation record (full create request, timestamps) before spawning `mngr create`, and passes an opaque workspace identity as a `workspace-id` host label — so the association rides on the host and survives app crashes.
- At startup (only), a reconcile cross-references host records, workspace records, and pending-creation records for lima and docker (modal excluded — sandboxes self-expire; pool hosts have their own reconcile):
  - Completed hosts with a `workspace-id` label but no workspace association are adopted automatically; if the pending record is gone, they are adopted as account-less (private) workspaces the user can link later.
  - Half-built hosts (labeled, no completed workspace) older than a 60-minute grace window are destroyed automatically. The grace window covers the orphaned-subprocess edge after a crash. If the grace expires while the app stays open, nothing revisits the host until the next restart — the interrupted row remains visible for manual discard in the meantime (accepted gap).
  - FAILED/DESTROYED host records older than the provider's `destroyed_host_persisted_seconds` (7-day default) are deleted, since minds envs never run `mngr gc`.
  - Hosts without minds labels are never touched (log a warning only). No migration for pre-existing orphans; the changelog/docs describe manual cleanup via `mngr list` + `mngr destroy @<host-id>`.
  - All reconcile actions are log-only (no notifications); adopted workspaces just appear in the list.
- Pending-creation records are local-only; other devices see the workspace only once it exists. The quit flow is unchanged.

### mngr host-targeted destroy and lima hygiene

- `mngr destroy` accepts host addresses (`@HOST[.PROVIDER]`, bare `host-<id>`): it destroys the host and everything on it, requiring `--force` (or confirmation) whenever any agent exists on it. No `mngr stop` host addressing.
- `delete_host` on lima removes the VM definition itself (tolerating already-gone) before the disk and records, so gc's offline path no longer strands VMs in `limactl list`.
- Bare-image btrfs VMs stop failing `cloud-final` on every boot: mngr's in-guest format applies the `lima-<disk_name>` filesystem label Lima's boot script checks for, and an idempotent heal labels already-formatted disks — so every boot after the first takes Lima's happy path (no re-partitioning, no degraded state). First boot on a stock Debian image remains degraded (unavoidable: no provisioning hook runs before Lima's disk script, and no Debian genericcloud image ships btrfs-progs); the prebaked image eliminates even that.
- A transient image-download failure (TLS handshake timeout, connection reset, EOF, 5xx, context deadline exceeded) no longer kills the create: `limactl start` is retried once (2 attempts total), with the half-created instance cleaned up between attempts. Permanent failures (404/Not Found/403) are never retried. Lima's download cache is already atomic; no cache handling needed.

### Creation rows in the workspace list

- Every in-flight creation appears as a row in the workspace list, inline where the finished workspace would sort, visually badged (creating spinner / interrupted / failed). The row becomes the workspace in place: on DONE, the pending record is deleted only once the workspace appears in a discovery snapshot (no flicker, no timeout bound).
- Clicking a creating row returns to the creation-progress view; each creation's log lines are buffered in memory (capped at the last 10,000 lines, truncation marker on replay) so re-entering replays the buffer then streams live.
- After an app restart kills an in-flight create, its row shows as interrupted with retry and discard actions. Retry reopens the create form pre-filled from the pending record; submission first destroys any leftover half-built host and record, then starts the new create. Discard destroys the leftovers and removes the row.
- Failed creations persist as failed rows across restarts (the pending record stores the error message plus the last 1000 log lines); only dismiss deletes them.
- Dead (interrupted/failed) rows do not reserve their names. Submitting a create whose name and provider match a dead row implicitly discards it first — clean up, then try again. Cross-provider same-name dead rows stay until dismissed.
- Dismissing a row whose leftover host still exists shows a transient "cleaning up…" state with the destroy command's output visible (same pattern as workspace destruction today); a failed destroy leaves the row showing the error, dismiss is retryable, and the 7-day retention cleanup is the backstop.
- Canceling a live in-flight creation is deferred to a follow-up; creating rows are view-only.

## Changes

### PR 1 — mngr + mngr_lima fixes

- lima `create_host`: check host records for a non-destroyed, non-FAILED same-name host and raise `HostNameConflictError`; write a BUILDING reservation record before `limactl start`; make discovery report reservation records as BUILDING (today a config-less record reads as FAILED).
- `HostNameConflictError`: include the conflicting host's id and state, with the `mngr destroy @<host-id>` remediation for BUILDING conflicts.
- `mngr destroy`: accept host addresses through the existing agent-or-host address parser; resolve to the provider's host; destroy everything on it; `--force`/confirmation semantics when agents exist; refuse the local host; update command docs and help.
- lima `delete_host`: delete the Lima VM definition (force, tolerate not-found) before deleting the disk, records, volume, and keys.
- lima provisioning script: add the `lima-<disk_name>` label to the in-guest `mkfs.btrfs`, plus an idempotent label heal for already-formatted data disks.
- lima `create_host`: retry `limactl start` once on transient-classified download failures, with instance cleanup between attempts; never retry permanent HTTP failures.
- Unit/integration tests for conflict semantics, reservation records, address parsing, retry classification, and script generation; ratchet updates; changelog entries for `libs/mngr` and `libs/mngr_lima`.

### PR 2 — minds reliability fixes

- Readiness: 900s hardcoded slow-path window applied when a LIMA create resolved no prebaked image; STUCK suppression tied to in-flight creations with the agreed end conditions.
- Pending-creation records: a local store writing the full create request before the `mngr create` subprocess spawns; records survive FAILED (storing the error and last 1000 log lines); deleted on discovery-confirmed DONE.
- Creates pass the `workspace-id` host label for lima and docker launches.
- Startup reconcile implementing the adopt / destroy-after-grace / retention-cleanup / warn-unlabeled policy above, shelling out to `mngr destroy @<host-id>.<provider> --force` for host teardown.
- Name checks: form availability, `start_creation`, and the auto-namer consult live in-flight creations.
- Unit/integration tests for the record store, reconcile policy (against mock discovery/provider data), and name checks; changelog entry for `apps/minds`.

### PR 3 — creation rows UX

- Workspace list rows for creating/interrupted/failed states, inline and badged, backed by pending records plus live creation status; discovery-confirmed hand-off to the real workspace row.
- Re-enterable creation-progress view with the 10,000-line in-memory log buffer and replay.
- Interrupted-row retry (pre-filled create form; destructive cleanup on submit) and discard; failed-row error display and dismiss; provider-scoped implicit discard on same-name resubmit; "cleaning up…" state streaming the destroy output.
- Unit/integration tests for row state derivation and the discard/retry flows (no new e2e); changelog entry for `apps/minds`.
