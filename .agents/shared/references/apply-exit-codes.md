# The apply: what each exit means

`update_self.py apply` lands a merged branch on the live workspace as one
motion: it merges, rebuilds and restarts what changed, probes the workspace's
health, and reverts the entire merge when any of that fails. This file says
what the apply has done when it exits, whoever ran it. What your flow does
next (whether to retry, what to tell the user, what to record) is in your own
guide.

Read the closing stderr lines before acting on the exit code: they say what
the apply did and name any follow-up.

## Contents

- Exit 0: applied
- Exit 2: automatically rolled back
- Exit 3: emergency
- Exit 1: precondition, nothing changed
- An interrupted apply
- The phase timings line

## Exit 0: applied

The merge is landed and the live workspace confirmed healthy. Three closing
lines change what that means:

- **A breakage that was already there.** A workspace whose frontend was
  broken before the apply still lands and still exits 0, but the final line
  names the breakage instead of confirming health. Pass it on as a separate
  problem; never repackage it as success.
- **A non-fatal warning** rides on stderr with its own follow-up. Carry it
  out or surface it.
- **`applied with incomplete provisioning`.** A provisioner run
  (`system/scripts/setup_system.sh`) that fails does not roll the merge back
  on its own: a failed tool install leaves the tree and services consistent,
  so the apply carries on to the restart and the probes, and a load-bearing
  provisioner change still fails those and still rolls back. When the probes
  pass over a failed provisioner, the gap is recorded at
  `data/.state/update-apply/provision-incomplete.json` (reason, merge, agent).
  That record is yours to close: diagnose the failure from the stderr excerpt
  (often no network, or a download that never completed), fix the cause, and
  re-run `bash system/scripts/setup_system.sh` by hand (idempotent). Only an
  apply's own successful provisioner run clears the record automatically, so
  once your manual run exits 0, `rm` it yourself. Tell the user the change is
  in and working but one tool-install step is still pending, never that
  everything completed.

## Exit 2: automatically rolled back

The apply reverted the **entire landed merge, every class and not just the
failing one**, as a forward revert commit, restored the pre-apply state, and
confirmed the workspace healthy on the previous revision. The change did not
land. If the closing line says the frontend was already broken beforehand,
that is a separate problem to report alongside.

A forward step that outlived its wall-clock budget (`<step> did not finish
within <N>s`) is one cause: the apply never waits open-endedly on a hung `npm
ci`, build, refresh, provisioner or restart, so read that message as "it
hung", with the timings line saying where.

Because the rollback is a forward revert, the merge stays in history while its
content does not, and re-running the apply of that same merge is refused (exit
1, nothing changed).

## Exit 3: emergency

Even the rollback could not restore a healthy workspace. Escalate
immediately. The pre-apply copies are kept under
`data/.state/update-apply/snapshots/`, and when the apply touched the frontend
the stderr names the bundle copy. Putting that one back is a plain file copy
needing neither npm nor a registry, so pass the path on. Read the stderr
rather than assuming a path is there: after a backend-only or provisioner-only
apply it names none.

The apply leaves `data/.state/update-apply/emergency.json` beside its marker,
naming the reason, the driving agent, and where the copies are. The shell
shows a banner for as long as that file exists, and nothing takes it down by
itself, so **clearing it is part of the repair**. Only an outcome that ends
with the live workspace *confirmed healthy*, frontend included, clears it: a
later apply that lands over a UI it could confirm, or a `recover` of a *new*
interruption that probes the workspace afterwards. Neither is the ordinary
case: the boot-time recovery runs before anything is up and has nothing to
probe, and an apply over a UI that was already broken exits 0 naming that
breakage rather than confirming health. This exit already cleared the marker
and its rollback already put the tree content back, so a bare `recover` finds
nothing to do and re-running the same `apply` is refused. When the repair was
by hand: verify the workspace is actually healthy, then delete
`emergency.json` and tell the user what happened. Leaving it in place means a
workspace that is fine goes on telling its user it may be broken.

## Exit 1: precondition, nothing changed

One of: a dirty tree; a merge that conflicts, or a fast-forward that is
refused because `HEAD` moved; another apply in flight; an interrupted apply of
a *different* merge, which needs `recover` first; or this merge having already
been landed **and rolled back** (exit 2). The stderr says which.

## An interrupted apply

Nothing is stranded. The apply writes its marker under
`data/.state/update-apply/` before the merge, updates it per phase, and every
step tolerates re-entry. So when you come back, **re-run the exact same
`apply` command**: it resumes from the recorded state.

If nobody comes back, the workspace heals itself: bootstrap rolls a stale
marker back at the next container start, and a recovery cron (`recover
--if-stale`) does the same within minutes for a kill without a restart. Both
leave the tree matching the revision from before the apply, with the branch
that was being applied intact. That rollback is a forward revert too, so the
same merge cannot be applied again, as after exit 2.

## The phase timings line

Every apply prints one `apply phase timings:` line: the duration of each
phase, from the apply's marker. When an apply took unusually long, quote it
rather than guessing which step was slow.
