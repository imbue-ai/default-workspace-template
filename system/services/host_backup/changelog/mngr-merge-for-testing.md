Testing branch merging `mngr/avoid-api-key-limits` and `mngr/remote-execution`. For host_backup it carries the backup-store branch's storage-quota handling unchanged, beside main's in-process env-converge capture; see that branch's entry.

The backup store's usage probe sends a `User-Agent` of its own (`host-backup/1.0`): Cloudflare answered urllib's default `Python-urllib/3.x` agent with a 403 ahead of the Worker, so every tick logged "The backup store refused the usage probe" and proceeded without quota information, and an over-quota account never took the skip-forget-prune path.
