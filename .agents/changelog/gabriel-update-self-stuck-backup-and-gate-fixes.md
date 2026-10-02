update-self's worker guide reserves `stuck` for a provisioning change whose absence would break the running workspace. A global pin bump cannot be held back (the apply re-runs the provisioner with every pin from the merged tree), so a user-created dependent of one is `done`, reported as "applied live, unverified in the worktree; the lead checks it after the apply".

The test gate for a commit is exactly what `select-tests` prints for it; a suite it did not print is a widening that goes through the worker's `question` gate. The shared harden guide says so once, and the lead's gate rule now says a `select-tests` output is never the "silence" its more-coverage fallback applies to.

Before changing a test that failed, a harden worker runs it on the tree before its change and reports whether the change caused the failure or it was already there (shared harden guide, "The test gate"); update-self's worker names `$MERGE^1` as that tree.

`create_worker.py revive` is `stop`'s inverse: it starts the stopped worker and blanks the `archived_at` label the stop set. lead-proxy's "Resuming after the user overrides a failure" uses it; update-self's "Resuming after `stuck`" lists only the flow's own preconditions (lease, run record, history bridge). launch-task's recovery docs treat a STOPPED worker as deliberately stopped only when `archived_at` is non-empty.

update-self's Step 1 no longer triggers a backup and waits for it. It runs `host-backup-now --check`, which reads the backup service's log and reports whether a recent restore point exists, with the same exit codes as before; the apply has no backup step.
