Backups now skip the contents of any directory holding a `.nobackup` file (restic's `--exclude-if-present`), keeping only that file. This is how an app marks a store it can rebuild, such as downloaded archives or clones, so it stops slowing every hourly backup: restic's time follows the number of files it walks, not their size. `CACHEDIR.TAG` is deliberately not honored, because uv writes one into every virtualenv and tool environment, which a restore could not refill.

`backup.toml` accepts `extra_excludes`, patterns excluded on top of the defaults. Setting `excludes` still replaces the defaults, so a single pattern no longer means copying the whole default list.

A backup that takes longer than `slow_backup_threshold_seconds` (default 300) records a `BACKUP_SLOW` event, at most once a day. It carries restic's file counts and names the next step.

New `uv run host-backup-heavy-dirs` lists the latest snapshot and prints the directories holding the most files and directories, nested under their parents, so the tree behind a slow backup can be found in one command.

The README opens with a "Slow backups" section: how to confirm the cause, find the tree, exclude it, and optionally rewrite it out of old snapshots so the daily prune stops walking it.
