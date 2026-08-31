Fix env-converge losing user-installed packages on a fresh rootfs (issue #523): `env-converge capture` (the command the apt Post-Invoke hook invokes) now skips on a rootfs without the converge identity stamp, so the units' own apt installs can no longer rewrite the record before the slow phase replays it; `--force` overrides for deliberate operator use, and the skip emits a `capture_skipped_fresh_rootfs` event.

The slow phase now snapshots the record into memory before the units run and replays from that snapshot, so nothing that fires the capture hook can change what gets replayed.

On the fresh-rootfs path, the final capture preserves recorded entries the replay could not install (transient failure, or a name gone from the pinned snapshot): they stay in the record and replay again on the next fresh boot instead of being silently dropped.

A failed multi-package apt/npm batch install now falls back to one install per entry, so a single unavailable name no longer sinks the whole batch.
