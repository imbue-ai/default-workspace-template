# Slow-path rebuilds carved the legacy home layout

Workspaces that minds created on imbue cloud through the slow path ended up with
their whole home tree in the container's writable layer instead of on the
persistent volume, and with backups failing every hour. This page records what
happened, how to recognize an affected workspace, and how to repair it.

## Status

- Found 2026-09-09 while investigating a "backups have never run" report on one
  workspace (`spare-051`).
- The root cause, in the mngr client's slow-path rebuild, is fixed by PR #902
  (`libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/providers/rebuild.py` forwards
  the whole `VpsProviderConfig` surface instead of a hand-copied subset). Until
  that lands and ships in a client release, every slow-path create still
  produces a legacy-layout workspace; hosts rebuilt before it keep the legacy
  layout until repaired.
- Repair is `minds-admin repair-home-layout`, described below, rehearsed on
  `spare-051` on 2026-09-09. Affected hosts are repaired individually, with
  the owner told when their workspace will be quiet for a few minutes.

## Symptom

Inside the workspace, `host_backup` logs on every tick:

```
restic backup failed (rc=1): /mngr-snapshots/<ts>/home does not exist, skipping
{"message_type":"exit_error","code":1,"message":"Fatal: all source directories/files do not exist"}
host_backup has failed N consecutive ticks
```

Nothing surfaces this to the user. The service reads RUNNING in supervisord,
the workspace works normally, and the backup history page in the app shows
either no snapshots or only the ones from before the rebuild.

## Two layouts

A slice's persistent volume is one btrfs subvolume, and the outer snapshot
helper snapshots the whole subvolume for every backup. What is in it depends on
how the container was created:

- **Bake layout** (every pool host baked since minds-v0.3.10): the volume holds
  `home/`, the container's `/home/user` is a symlink onto it, and the mngr
  host_dir is the plain directory `home/.mngr`. A backup of `<snapshot>/home`
  covers everything.
- **Legacy layout**: the volume holds only `host_dir/`, symlinked from the
  container's `/home/user/.mngr`. `/home/user` itself is a real directory in
  the container's writable layer. A backup of `<snapshot>/home` finds nothing.

Pre-declutter bakes (minds-v0.3.9 and earlier) also have the `host_dir/`-only
volume, but that generation keeps the workspace under `/mngr` on the volume and
runs an older host_backup that does not assume `home/`. Their data is
persisted and their backups work. They are not affected, as long as nobody
updates their backup service to a newer tag.

## Root cause

minds tries the fast path first (adopt a pre-baked pool host whose template tag
matches the client's) and falls back to the slow path when none is available,
which is what happens right after a version bump before hosts of the new tag
are baked. The slow path leases any host, tears down its baked container, and
rebuilds it through a delegated provider. The two builders in `rebuild.py`
constructed that provider's config by copying individual fields off the
account block and copied `host_dir` but not `volume_home_path` or
`host_log_dir`, so the realizer carved the legacy layout.

The account block minds writes has carried `volume_home_path = "/home/user"`
since the workspace-root declutter (July 2026), so every slow-path create since
then produced a legacy-layout workspace.

## Recognizing an affected workspace

From the laptop, with the production pool key and DSN:

```sh
export MINDS_HOST_POOL_DSN="$(vault kv get -mount=secrets -field=value minds/production/neon/DATABASE_URL)"
export POOL_SSH_PRIVATE_KEY="$(vault kv get -mount=secrets -field=value minds/production/pool-ssh/POOL_SSH_PRIVATE_KEY)"
uv run minds-admin repair-home-layout --host-id host-<hex>
```

The default action probes and reports `home_layout` or `legacy_layout` per
host, with the size of the container's home tree and the free space on the
data disk. Nothing is changed. `--all-leased` probes every leased slice in the
pool instead of named hosts (probe only), which is how the affected set is
found in the first place.

A cruder DB signal: a leased host whose active workspace record's `agent_id`
differs from `pool_hosts.agent_id` was rebuilt rather than adopted. It is a
proxy, not a verdict; probe before acting.

Measured on 2026-09-09 across 226 leased hosts: 134 on the bake layout, 21
legacy with backups failing (20 rebuilds plus one pre-declutter host whose
backup service had been updated), 63 legacy with the older backup service still
working, 5 legacy with no backup activity, 3 legacy failing for unrelated
reasons.

## What is and is not at risk

- **Stop and start, including a relocation to another box, lose nothing.** The
  stop artifact is the VM's whole root disk (which holds docker's storage and so
  the container's writable layer) plus the data disk; start boots the same
  disks and `docker start`s the same container.
- **A restore from backup loses the home tree.** With the layout as it is, the
  restic repository holds host_dir only: chat history and agent state, not the
  workspace checkout, apps, skills, or dotfiles.
- **The hourly backup fails** until the layout is repaired (or the backup
  service is taught to read `host_dir/`, which would still leave the home tree
  out).

## Repairing an affected workspace

`minds-admin repair-home-layout --host-id <id> --migrate` runs one script as
root in the slice VM:

1. Preflight: legacy layout, `home/` absent on the volume, `/home/user/.mngr`
   resolving to `/mngr-vol/host_dir`, and free space on the data disk of at
   least the home tree plus 10 percent plus 1 GiB. Any failure here refuses
   with nothing changed.
2. Quiesce, inside the container with the workspace's env file sourced: stop
   every non-main agent that is not already STOPPED through the workspace's
   own mngr (an idle chat still holds a claude process rooted in the home
   tree), `supervisorctl stop all`, and wait for an in-flight restic run to
   finish.
3. Take a read-only btrfs snapshot of the host subvolume under
   `<data mount>/rollback/pre-home-layout-<stamp>`. It sits outside the
   `snapshots/` directory host_backup manages, so nothing reaps it.
4. `docker cp` the container's `/home/user` onto the volume, verify the copy
   by file count, move it into place as `home/`, drop the copied `.mngr`
   symlink, rename `host_dir/` to `home/.mngr` (a rename within one
   subvolume), and leave an empty `host_dir/` behind as a bake does.
5. In the container, move each entry of `/home/user` into
   `/home/user.pre-migration` (renames, except for entries that exist in the
   image layer, which overlayfs will not rename and which are copied after a
   root-disk space check), then symlink `/home/user` onto `/mngr-vol/home`.
   Every absolute path anyone recorded still resolves.
6. `supervisorctl restart all` and wait for `system_interface` and
   `host-backup` to read RUNNING.

The workspace is unavailable from step 2 to step 6, a few minutes for a few
gigabytes. The next host_backup tick finds `<snapshot>/home` and backs up the
whole tree. The command prints a JSON outcome per host and exits non-zero if
any host failed. The outcome names any process still rooted in the old tree;
the system-services chain (the tmux session, bootstrap, supervisord and their
shells) stays there until the container next restarts and is harmless, since
the supervised programs are restarted with the new path.

`--rollback` reverses steps 4 and 5 while `/home/user.pre-migration` and the
`home/.mngr` directory still exist, after the same quiesce. The migrated copy is
kept beside the volume as `.home-rolled-back-<stamp>`.

Cleanup, after a few days of the workspace running normally: delete the
rollback snapshot (`btrfs subvolume delete <data mount>/rollback/pre-home-layout-<stamp>`)
and the container's `/home/user.pre-migration`. Neither is done automatically.

Rehearsed on `spare-051` on 2026-09-09: 5.1 GB home tree, 17 entries renamed
and none copied, services back within a minute, and the next backup tick
produced a restic snapshot of `<snapshot>/home` in about 70 seconds. Two
earlier attempts on the same host failed safely and shaped the procedure: a
whole-directory `mv` of `/home/user` is not a rename on overlayfs and copied
the tree onto the VM's root disk until it filled, and the snapshot
destination must be on the btrfs data mount rather than docker's volume path.

## terrapintrail2

One pre-declutter host had received a `backup-update: minds-v0.3.11` commit
through the app's "Update backup service" operation, which installed the
`home/`-assuming host_backup on its `host_dir/`-only volume. Repaired by hand
on 2026-09-10: revert that commit in `/mngr/code`, `uv sync --all-packages`,
copy `data/.secrets/restic.env` back to the pre-declutter path
`runtime/secrets/restic.env` (the update had re-injected it at the new path),
and restart `host-backup`. Its first successful backup in 35 days followed.

## Fleet run, 2026-09-10

Of the 21 hosts the 2026-09-09 catalogue listed as legacy with backups
failing: terrapintrail2 was repaired by reverting its backup-service update;
contract-tracker is a July rebuild whose home lives under `/mngr` and probes
as an unrecognized layout, so it was left alone; two had been destroyed by
their owner before the run; 16 were migrated in place with
`repair-home-layout --migrate`, one at a time, each verified by a new
successful backup tick within a minute of
its restart; and one (nicholas's `workspace-4`,
`host-c22f7e3f738247ac826758d3925ae201`) was deferred because a chat was
actively generating each time it was tried, and is still on the legacy layout
with its hourly backups failing. No retry is running for it: run the command again
when its owner is idle. Home trees ranged
from 0.9 GB to 3.4 GB and no host needed the image-layer copy path (every
entry renamed). The nine older rebuilds from June and July probe as an
unrecognized layout and were left alone: that generation keeps its workspace
under `/mngr` on the volume, so they are persisted and backing up already.

Each migrated host keeps its rollback snapshot under `<data mount>/rollback/`
and its aside copy at `/home/user.pre-migration`; the cleanup pass above is
still owed.

## Stopped hosts

A stopped host has no running VM to probe. Its layout is whatever it had when
it stopped, preserved in the artifact. There is no operator start route: the
connector's `POST /workspaces/{id}/start` resolves the caller as the owner and
`minds-admin workspaces` offers only stop, abandon and release. To start one
without the owner, move its row into the transition the connector's hourly
watchdog re-drives (the same recovery path a crashed supervisor takes):

```sql
UPDATE pool_hosts SET status = 'starting', transition_error = NULL,
  transition_failure_count = 0, transition_id = '<fresh uuid>',
  transition_heartbeat_at = NULL
 WHERE host_id = '<host-id>' AND status = 'stopped' AND artifact_manifest IS NOT NULL;
```

A null heartbeat makes the watchdog (`modal.Cron("45 * * * *")` on the
connector) claim the row on its next run and start it: in place on its origin
box when the halted VM is still there (a start inside the stop's retention
window), otherwise by restoring the artifact onto a same-region box. Either
way the row lands on `leased` some minutes later. This bypasses
the owner route's running-workspace quota check. Probe and repair the host
once it is leased, then `minds-admin workspaces stop <host_db_id>` returns it
to stopped. Done this way for `test-051` on 2026-09-10.

### Stopped-host run, 2026-09-10

Seven stopped hosts carried the rebuilt/mixed signature. `test-051`,
`bullet-journal-cloud`, `fresh-044` and `new-multiminds-044` (all leased on
their own accounts) were started through the watchdog, migrated, verified by a
new backup tick, and stopped again. `product-minds-old` (baked `minds-v0.3.4`)
and `slack-recordings` (`minds-v0.3.6`) are pre-declutter rebuilds whose home
lives under `/mngr`, out of scope like the leased ones of that generation, and
were left stopped and untouched.

The seventh, `workspace-1` (`host-e837cd4e399b48508116e85fa49081ab`, a
different owner), could not migrate: its own session had filled the container's
32 GB root disk to ~250 MB free with a `/tmp` disk-benchmark workload
(`dd` of five 1 GiB images, then `restic backup /tmp`), and every entry of
`/home/user` renamed rather than copied, so the switch step tripped an
over-strict free-space check -- it demanded a 512 MiB margin even though it had
nothing to copy. The migration rolled back cleanly (host_dir restored, still on
the legacy layout, no data touched), but the restart afterwards left it with a
full root disk and the same workload immediately re-running, so its services
did not come back up on their own. It was cleaned up (drop the unused prior
template image to free the root disk, remove the failed run's `home/` copy and
rollback snapshot, restart `system-services`) and stopped again, returning it
to the state its owner left it in -- still on the legacy layout.

The free-space check now applies only when there are image-layer entries to
copy, so a rename-only switch no longer fails on a full root disk. `workspace-1`
was not retried: it is another user's workspace and starting it re-runs that
disk-filling workload.
