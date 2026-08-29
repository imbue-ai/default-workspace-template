# Apply outcomes, records, and rollbacks

What `update_self.py apply` leaves behind on each exit, what to tell the user,
and how to act on the records it writes under `data/.state/update-apply/`.

## Exit 0 -- applied

The update is landed, recorded in `docs/VERSION_HISTORY.md`, and the live
workspace confirmed healthy; the environment advanced to the merged apt
snapshot (summarize the env-converge delta in plain language). Read the
closing stderr lines before signing off:

- A workspace whose frontend was already broken beforehand still lands and
  still exits 0, but the final line names the breakage instead of confirming
  health. Pass it on as a separate problem; never repackage it as success.
- A non-fatal warning (the ledger could not be committed, `env-converge
  upgrade` failed) rides on stderr with its own follow-up; carry it out or
  surface it.
- **`applied with incomplete provisioning`**: a provisioner run
  (`system/scripts/setup_system.sh`) that fails does not roll the update back
  on its own -- a failed tool install leaves the tree and services consistent,
  so the apply carries on to the restart and the probes, and a load-bearing
  provisioner change still fails those and still rolls back. When the probes
  pass over a failed provisioner, the gap is recorded at
  `data/.state/update-apply/provision-incomplete.json` (reason, merge, agent).
  That record is yours to close: diagnose the failure from the stderr excerpt
  (often no network, or a download that never completed), fix the cause, and
  re-run `bash system/scripts/setup_system.sh` by hand (idempotent). Only an
  apply's own successful provisioner run clears the record automatically, so
  once your manual run exits 0, `rm` it yourself. Tell the user the update is
  in and working but one tool-install step is still pending -- never that
  everything completed.

Every apply also prints one `apply phase timings:` line -- per-phase durations
from the apply marker, the benchmarking input for the apply's poll and step
budgets. When an apply took unusually long, quote it rather than guessing
which step was slow.

## Exit 2 -- automatically rolled back

The apply reverted the **entire landed merge -- every class, not just the
failing one** -- as a forward revert commit, restored the pre-apply state, and
confirmed the workspace healthy on the previous revision. The update did not
land. A forward step that outlived its wall-clock budget (`<step> did not
finish within <N>s`) is one cause: the apply never waits open-endedly on a
hung `npm ci`, build, refresh, provisioner or restart, so treat that message
as "it hung", with the timings line saying where.

The message you compose carries three things, in order: **the workspace is
safe** ("the update hit a problem while being applied, so I automatically
undid it -- everything is back exactly as it was, and nothing is broken");
**what failed**, in plain terms, at whatever level of cause the stderr
established; and **the way forward**. The retry path survives every rollback
by design -- the worker's branch, worktree and report are kept -- so once the
failure is diagnosed the re-land is quick: offer exactly that ("I can look into
what went wrong and try again once it's fixed"), and never make the user feel
the whole pass must be redone. If the closing line said the frontend was
already broken beforehand, report that alongside. Record `run-status verdict
REFUSED --detail "<what failed, one plain line>"`.

Because the rollback is a forward revert, the merge stays in history while its
content does not, and re-running the apply of that same merge is refused. A
retry means a fresh worker pass off the current `HEAD`.

## Exit 3 -- emergency

Even the rollback could not restore a healthy workspace. Escalate immediately.
The pre-apply copies are kept under `data/.state/update-apply/snapshots/`, and
when the apply touched the frontend the stderr names the bundle copy --
putting that one back is a plain file copy needing neither npm nor a registry,
so pass the path on. Read the stderr rather than assuming a path is there;
after a backend-only or provisioner-only apply it names none.

The apply leaves `data/.state/update-apply/emergency.json` beside the marker
naming the reason, the driving agent, and where the copies are; the system
interface shows a banner keyed off its mere presence. Nothing takes the record
down by itself, so **clearing it is part of the repair**. Only an outcome that
ends with the live workspace *confirmed healthy*, frontend included, clears it:
a later update that lands over a UI it could confirm, or a `recover` of a
*new* interruption that probes the workspace afterwards. Neither is the
ordinary case -- the boot-time recovery runs before anything is up and has
nothing to probe, and an apply over a UI that was already broken exits 0
naming that breakage rather than confirming health. This exit already cleared
the marker and its rollback already put the tree content back, so a bare
`recover` finds nothing to do and re-running the same `apply` is refused. When
the repair was by hand: verify the workspace is actually healthy, then delete
`emergency.json` and tell the user what happened. Leaving it in place means a
workspace that is fine goes on telling its user it may be broken.

## Exit 1 -- precondition; nothing changed

A dirty tree; a refused fast-forward (`HEAD` moved under the pass -- treat as
stale per `.agents/shared/references/harden-contention.md` and re-dispatch off
the current `HEAD`, never hand-resolve); another apply in flight; an
interrupted apply of a *different* merge that needs `recover` first; or this
merge having already been landed **and rolled back** (see exit 2). Re-dispatch
a fresh worker pass off the current `HEAD`.

## An interrupted apply

Nothing is stranded. The apply writes its marker under
`data/.state/update-apply/` before the merge, updates it per phase, and every
step tolerates re-entry -- so when you come back, **re-run the exact same
`apply` command**; it resumes from the recorded state. If nobody comes back
the workspace heals itself: bootstrap rolls a stale marker back at the next
container start, and a recovery cron (`recover --if-stale`) does the same
within minutes for a kill without a restart. Both leave the tree matching the
pre-update revision with the worker branch intact; as above, that is a forward
revert, so a retry means a fresh worker pass.

One carve-out, exactly once per workspace: both unattended triggers live in the
*running* container, not in the merge being applied. On the very first apply
that lands this machinery into a workspace, neither exists yet, and an
interruption before it restarts is only recoverable by re-running the apply
by hand. Say so to the user if that is the update you are running.

## Rolling back on request

The results message always offers a rollback, and the offer must be real. If
the user wants the update (or one piece of it) gone: create a **forward
revert** on a branch -- `git revert -m 1 <merge sha>` for the whole update, or
a commit reverting just the paths they dislike; never rewind history -- and
land it with the same machinery:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py apply \
    --merge-ref <that branch>
```

(ordinary merge mode, no `--target-ref`), so the revert gets the same refresh,
restart and health-probe motion the update got. Two residues to mention when
they matter: the apt snapshot advanced by `env-converge upgrade` stays
advanced, and the version-history entry stays (the revert is its own history).
The full-rewind fallback is the Step 1 backup, when one was captured.

## Migration-required updates

Some updates cannot be applied in place -- the judgment is the lead's,
standardized here rather than by any mechanical marker: the release
restructures something this workspace's live state was built on (a changed
data layout with no in-place migration, a provisioning change only a fresh
container can adopt that the workspace genuinely needs), or the worker's
`stuck` report shows the merge cannot land without breaking the running
workspace. Keep the user-facing message high-level -- no path tables, no
hand-rolled migration plans:

> This update contains fundamental changes that can't be directly applied to a
> running workspace. To take it: (1) create a new workspace, then (2) message
> its agent `/migrate-workspace <this workspace's name>` -- it will pull your
> work, apps, and settings across.

If a newer in-place-compatible release also exists (the incompatibility starts
at some later version), offer both in the same breath: "I can apply <X> now;
<Y> needs the fresh-workspace migration." Land the compatible one with
`--override` if the user takes that option. Record `run-status verdict
NEEDS_RECREATION --detail "<why, one plain line>"`, with
`--in-place-compatible-ref <X>` when that release exists -- the app's modal
offers the migration handoff off exactly this verdict.
