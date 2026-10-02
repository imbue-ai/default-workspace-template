Only the newest tick to start counts as in flight: the service runs one tick at a time, so an earlier tick with no terminal event was killed mid-run and will never finish. `host-backup-now` no longer waits on such a tick, and the service records it as `TICK_ABANDONED` when it starts again, so the events log holds no tick that never ends. The wait for a triggered tick skips that record and reports the restarted service's first tick.

When the in-flight tick is still running at the timeout, `host-backup-now` exits 2 without triggering a tick nobody waits for.

New `host-backup-now --check` triggers nothing and waits for nothing: it prints the newest tick outcomes and exits 0 when a `restic_backup_succeeded` is within two backup intervals, 3 when the newest tick ended for missing secrets, and 1 otherwise. update-self runs it before an update as its restore-point check.
