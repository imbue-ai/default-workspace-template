`host-backup-now` no longer waits on a backup tick that will never finish. A tick killed mid-run (an OOM shed, the update's services restart) leaves no terminal event, and the command used to treat it as in flight until it scrolled out of the last 200 events, about 25 ticks later, holding every call for its full 30-minute timeout. Only the newest tick to start can be in flight now, since the service runs one tick at a time.

When the in-flight tick is still running at the timeout, `host-backup-now` exits 2 without triggering a new tick, instead of starting one nobody was waiting for.

New `host-backup-now --wait-only` waits for the in-flight tick and triggers nothing, printing `{"inflight_tick_id": ..., "finished": ...}` and exiting 0 once no tick is in flight or 2 when it was still running at the timeout. With `--timeout 0` it only reports; the update apply runs it that way before restarting the services, to note a tick the restart interrupts.

The service now records a tick it was killed in as `TICK_ABANDONED` when it starts again, so the events log no longer holds a tick that never ends. The wait for a triggered tick skips that record and reports the restarted service's first tick, which backs up again, instead of waiting out its timeout.
