update-self no longer stops on a global pin bump that something the user built depends on. The worker guide now says such a dependent is "applied live, unverified in the worktree; the lead checks it after the apply", reported under Provisioning changes, and reserves `stuck` for one condition: leaving the running workspace on the old provisioning would break it. The apply re-runs the provisioner with every pin from the merged tree, so holding one pin back was never possible. A lead that receives a `stuck` for a rebuild-only or unverifiable-dependent finding sends it back to the worker.

A commit the worker makes after its test suites ran is gated by `select-tests` for that commit alone; re-running a whole suite on top is a scope widening that goes through the worker's question gate, and the lead no longer asks for one.

Before changing a test that failed after the merge, the worker runs it on the pre-merge tree and reports whether the update caused the failure or it was already there.

Resolving the merge is governed by the worker guide, not update-app, so the worker has no "lapse" to report for not reading another skill, and the lead relays a worker's self-reported deviation only when it changed the outcome.

SKILL.md has a "Resuming after `stuck`" path for when the user overrides the verdict: re-take the lease, record the run, rebuild the history bridge, revive the stopped worker with `mngr start update-self --restart`, reply with the user's decision, and re-arm the poll. The lead never runs the worker's validation itself. launch-task's worker-failure reference names the same revival.

Step 1's backup now runs in the background through `run_in_background.py` (`host-backup-now --timeout 1800`) and overlaps the worker; the lead reads its exit code before the apply, so the results message reports the real outcome.

The apply waits up to 15 minutes for a backup tick already in flight before restarting the services, which used to kill it mid-restic, and says on stderr whether it waited, interrupted the tick, or could not check; an interrupted tick is a caveat in the results message.
