Reworked the workspace stop/start state machine so a persistent failure can no longer become a silent, unrecoverable wedge (imbue-ai/mngr-internal#547):

- A stop now lands the row on `stopped` the moment its upload verifies, instead of sitting in `stopping` for the whole retention window. The halted local VM (and its placement/box link) is kept on the row through the window for fast restart-in-place, then reaped by the supervisor's retention-finalize phase (with the watchdog as backstop).

- A failed start always lands back on `stopped` with the error recorded and placement untouched -- the old two-branch fallback that bounced an in-window restart failure back to `stopping` (making every retry round-trip) is gone.

- Transitions only begin from stable states: `POST /workspaces/{id}/start` on a still-`stopping` row now answers 409 (naming the current status) instead of preempting the stop supervisor, so stop and start supervisors can never run concurrently.

- Supervisors are fenced by a `transition_id` ownership token (new column, migration 029): every supervisor write -- heartbeats, recorded material, final CASes, and `transition_error` -- is guarded on it, so a superseded or taken-over driver's writes hit zero rows and it exits. In particular, a stale stop supervisor can no longer stamp its failure onto a row that is now `starting`.

- The watchdog cron now *takes over* orphaned transitions under a fresh fencing token instead of spawning duelling supervisors, backs off exponentially in the new `transition_failure_count` column, and escalates to ops (error-level log, reaching the tier's error tracker) once a transition has failed many consecutive times.

- Box command sequences (VM stop, restart-in-place, finalize, rollback) run as one `&&`-joined command over a single management SSH connection instead of one connection per command, cutting control-plane load on the box.

- A hard reserve failure on one restore candidate box (e.g. a drifted box missing its transfer tooling) no longer fails the whole start: the claim is rolled back and the remaining candidates are tried, with the last failure reported only when every box refused.

- The box reconcile sweep's health probe now also flags boxes missing the workspace transfer tooling (`s5cmd`/`age`/`zstd`), so provisioning drift surfaces before a stop or restore lands on the box and fails.
