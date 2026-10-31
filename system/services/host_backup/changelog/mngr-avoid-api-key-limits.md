Backups now respect the account's Imbue Cloud storage quota before uploading. When the repository is a `rest:` URL (Imbue Studio's backup store), each tick first asks the store `GET <repository>_usage`; over the soft quota the tick records a new `backup_skipped_over_quota` event (with `used_bytes`, `account_used_bytes`, `quota_bytes`, `hard_limit_bytes`) and runs the retention forget plus `restic prune --max-repack-size <half the headroom>` to free space instead of backing up, until the account is under quota again. A server without the probe, or one that does not answer, means no quota information and the tick proceeds as before.

A backup the store refuses for lack of storage (restic's "507 Insufficient Storage") fails at once and its `restic_backup_failed` event carries `reason = "quota_exceeded"`.

`restic backup` pins `--pack-size 16`, and `restic prune` accepts a repack bound.
