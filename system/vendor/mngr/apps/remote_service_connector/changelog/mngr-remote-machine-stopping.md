Workspace stop/start lifecycle: leased slice workspaces can now be stopped (VM halted, disks encrypted and uploaded to the tier's OVH Object Storage bucket, bare-metal slot freed) and started again (in-place within the retention window, or restored onto any same-region box).

New `GET /workspaces` full-lifecycle listing plus async `POST /workspaces/{id}/stop|start`; `GET /hosts` stays leased-only and is deprecated in favor of `/workspaces`, to be removed once clients transition.

Transitions are driven by a spawned per-transition Modal supervisor (0.25 CPU / 512MB) with an hourly watchdog cron that re-drives orphaned transitions via row heartbeats; retries continue indefinitely and abandoning a row (new admin abandon endpoint -> status `crashed`) is always a manual operator action.

Artifacts are `zstd | age | s5cmd` streams with per-stop age identities wrapped by a tier KEK (envelope encryption); ciphertext sha256s are recorded in the row manifest and verified before boot. Re-stops keep only the newest generation; releasing/destroying a stopped workspace deletes its objects.

New `max_total_workspaces` entitlement (explorer 10 / ally 50) caps running + stopped workspaces, while `max_remote_workspaces` now caps running (leased/stopping/starting) only; `/account` usage reports both counts.

Releasing a `crashed` (operator-abandoned) workspace now attempts the VM teardown best-effort: an unreachable box is logged and the release still completes instead of wedging the row in `removing` forever; a reachable box still gets a normal teardown.

Artifact deletion (release, re-stop generation cleanup, canceled-upload cleanup) now surfaces S3 failures -- including quiet-mode per-key delete errors -- as a retryable failure instead of silently counting partially-deleted prefixes as reclaimed.
