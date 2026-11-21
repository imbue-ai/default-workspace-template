# Resuming after a restart

A note from the Imbue Studio app saying this chat stopped and was started again
means the pass was interrupted, most often by a workspace restart, which stops
every chat. Find where it got to before doing anything: re-read
`data/.state/update-apply/run.json` and this pass's task file, and check whether
the worker is still alive. A record showing an apply under way belongs to the
apply's own recovery (`apply-outcomes.md`, "An interrupted apply"). If the
workspace also sent you a note that the restart rolled the apply back, that
record is already cleared: follow `apply-outcomes.md`, "After a restart rolled
it back", and do not carry on into the apply or a retry.
Otherwise carry on from the step you were in; a worker that stopped too is
restarted per `.agents/skills/launch-task/references/dead-worker-recovery.md`.
The note counts the restarts so far. If this chat keeps stopping for a reason
you cannot find or fix, record `run-status verdict STUCK --detail "<why, one
plain line>"` and tell the user, or the app will keep restarting it.
