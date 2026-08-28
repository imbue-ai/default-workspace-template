Added `libs/mngr_tmr/scripts/prune_tmr_hosts.py`, which destroys the hosts that past TMR runs left behind.

TMR never destroys the hosts it creates, so their records accumulate on the provider's shared state volume, and every subsequent host creation reads all of them to check name uniqueness -- slow enough, at a few thousand records, to make host creation fail outright. The script keeps each variant's most recent run (so its mappers stay available to re-attach to for debugging) plus any run too young to be sure it has finished, destroys everything older, and purges the host records in the same sweep. It runs daily in CI ahead of the scheduled runs; `--dry-run` reports what it would destroy.

A run whose hosts have been pruned can no longer be reintegrated (`--reintegrate` rediscovers a run's mappers by label); pass `--keep-runs` to keep more than the most recent run around.
