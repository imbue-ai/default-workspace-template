# Lead-side proxy flow: diagnosing worker liveness

## A poll that timed out

If the timeout trips without a report appearing, the worker may still be
alive and working. Long-running stages (the test gate, large
implementations) can legitimately exceed 30 minutes on a healthy worker.
Before invoking the failure flow, check the worker session:

```bash
mngr capture <WORKER_NAME> --full --no-start | tail -40
```

If the output shows ongoing tool use, an active spinner / "Running…" line,
or recent timestamps within the last few minutes, the worker is alive --
re-arm the poll with a longer timeout (e.g. `60m`) and continue. A session
that is dead is a crash: recover the worker
(`.agents/shared/references/lead-proxy/dead-worker-recovery.md`) and re-arm
the poll. Only invoke the failure flow
(`.agents/shared/references/lead-proxy/worker-failure.md`) if that recovery
does not bring it back, the agent is wedged on the same operation for an
extended period, or output has been static.

## Exit code 76: the worker went idle without reporting

`await` makes the same call for you in the clearest case: exit code **76** means
it watched the worker's agent end its turn (or stop) on several consecutive
polls with no report and no longer busy -- finished, dead or wedged without
reporting, worth acting on immediately rather than waiting out the timeout, by
the diagnosis above: a dead worker is recovered before anything is surfaced.
That check never fires while the worker is waiting on something that will wake
it:

- **A live worker of its own.** An agent labelled `lead_agent=<worker>` in
  state RUNNING or WAITING with no pending shed counts as the worker being
  busy, so an intermediate lead waiting on its own worker is never mistaken for
  a dead one.
- **A background command of its own.** A command the worker started through
  `run_in_background.py`, or a background task of Claude's own, marks the
  worker's chat busy until its report reaches the worker, so a worker that
  ended its turn to wait on its tests is not idle. A worker needs no `sleep` to
  stay clear of the check.
