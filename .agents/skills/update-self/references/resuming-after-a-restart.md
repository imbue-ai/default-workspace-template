# Resuming after a restart

A note from the Imbue Studio app saying this chat stopped and was started again
means the pass was interrupted, most often by a workspace restart, which stops
every chat. Find where it got to before doing anything: re-read
`data/.state/update-apply/run.json` and this pass's task file, and check whether
the worker is still alive. When the record shows an apply under way, read
`apply-outcomes.md` ("An interrupted apply"): it belongs to the apply's own
recovery. Otherwise carry on from the step you were in; when the worker
stopped too, read
`data/.tasks/update-self/skill-at-target/.agents/shared/references/lead-proxy/dead-worker-recovery.md`
and restart it as it says.
The note counts the restarts so far. If this chat keeps stopping for a reason
you cannot find or fix, record `run-status verdict STUCK --detail "<why, one
plain line>"` and tell the user, or the app will keep restarting it.
