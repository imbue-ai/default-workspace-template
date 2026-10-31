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

**You judge the retry; nobody is there to ask.** Read the cause from the
stderr and the `apply phase timings:` line, then:

- **A local resource cause** (a step shed for memory, a full disk, a step that
  outlived its budget on a loaded workspace): lessen the load first, using
  SKILL.md Step 1's licence (pause more chats with `run-status stop-chats
  --agent <name>`, stop the browser and windowless apps, prune caches,
  worktrees and old snapshots), then retry.
- **A network cause** (a download, `npm ci` or `uv sync` that could not reach
  its registry): retry once.
- **Anything else**, or a retry that fails again: no retry. That includes a
  critical app of the user's that fails its probe: it blocks the update, and
  the results message says so.

A retry goes back to the worker, which still holds its branch and its
validation; it is never a re-run of the same apply (below). Consume the `done`
you applied, as SKILL.md 5b does before a catch-up, then send it the cause and
the retry through 5b's catch-up sender with `$RETRY` in place of `$CATCH_UP`,
re-arm the poll, and audit the `done` it sends back (5a) before 5b applies
again:

```bash
RETRY="The apply rolled back: <the cause, one plain line>. Retry per §8 of your worker guide, catching up to $(git rev-parse HEAD), then report done again."
```

Only a worker that is gone takes a fresh pass (SKILL.md Step 3b, with the same
`$REF`, the lease and the run record carried over). Nothing retries beyond
these rules, and nothing retries unwatched: a pass that fails after your best
effort records `run-status verdict REFUSED --detail "<what failed, one plain
line>"` at once, so the app shows the failure immediately.

The message you compose carries three things, in order: **the workspace is
safe** ("the update hit a problem while being applied, so I automatically
undid it -- everything is back exactly as it was, and nothing is broken");
**what failed**, in plain terms, at whatever level of cause the stderr
established, with what you already tried; and **the way forward**: what would
have to change before another attempt can succeed. If the closing line said the
frontend was already broken beforehand, report that alongside.

Because the rollback is a forward revert, the merge stays in history while its
content does not, and re-running the apply of that same merge is refused. The
worker's retry -- like any later pass, to this release or a newer one -- reverts
the rollback commit on its branch (the worker reference says how): git counts
the reverted content as already merged, so a plain merge lands only what the
target changed since. The apply refuses a merge ref that leaves the rollback in
place (exit 1, nothing changed), naming the commits to revert.

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

A dirty tree; a refused fast-forward (`HEAD` moved under the pass); another
apply in flight; an interrupted apply of a *different* merge that needs
`recover` first; or this merge having already been landed **and rolled back**
(see exit 2).

With the other chats paused, a dirty tree or a refused fast-forward means
something wrote after the pause (the user, or a chat they woke). Neither makes
the worker's validation worthless: release the `editing critical app` leases
you took for the apply (5b takes them again right before the next one), save
the new changes as 5b saves the paused chats' work, then run 5b's
fast-forward check again. If `HEAD` moved, catch the worker up to it as 5b
does and apply its new `done`; if not, apply the branch as it is. Never destroy the worker to re-dispatch,
and never build the catch-up merge yourself: the worker's merge is the one its
validation covers. Another apply in flight is the same once it finishes, and
so is an interrupted apply of a different merge once `recover` has rolled it
back, as the refusal says: either has likely moved `HEAD`, so come back
through 5b's fast-forward check. A merge already landed and rolled back, or
one that leaves an earlier update's rollback in place, goes back to the worker
as a retry does (exit 2).

## Needs a decision -- nothing applied

The worker found something the user built that the update cannot keep (its
§4b verdict), so the pass ended before any apply: the workspace is exactly as
it was, and the `NEEDS_DECISION` verdict's detail is what the app shows. What
the pass keeps for the decision is the stopped worker -- its branch
`mngr/update-self` with the validated merge, its worktree and its conversation
-- and this chat. It holds no lease and keeps no chat paused while it waits, so
the workspace runs as usual, and the user can answer whenever they like.

Their choices, as the decision message puts them (`results-message.md`): take
the update and lose the customization, keep the customization and skip the
update, or the worker's best adaptation. Skipping records `REFUSED`; either
other answer re-lands the kept pass (`resuming-after-a-decision.md`),
catching the worker up first when the workspace moved on meanwhile. A later
update started before they answer sets the kept pass aside (Step 3b archives
its branch), and the decision is then that update's to ask again.

## An interrupted apply

Nothing is stranded. The apply writes its marker under
`data/.state/update-apply/` before the merge, updates it per phase, and every
step tolerates re-entry -- so when you come back, **re-run the exact same
`apply` command**; it resumes from the recorded state. If nobody comes back
the workspace heals itself: bootstrap rolls a stale marker back at the next
container start, and a recovery cron (`recover --if-stale`) does the same
within minutes for a kill without a restart. Both leave the tree matching the
pre-update revision with the worker branch intact; as above, that is a forward
revert, so a retry goes back to the worker as after exit 2.

One carve-out, exactly once per workspace: both unattended triggers live in the
*running* container, not in the merge being applied. On the very first apply
that lands this machinery into a workspace, neither exists yet, and an
interruption before it restarts is only recoverable by re-running the apply
by hand. Say so to the user if that is the update you are running.

## Rolling back on request

The results message always offers a rollback, and the offer must be real. If
the user wants the update (or one piece of it) gone: create a **forward
revert** on a branch -- `git revert -m 1 <merge sha>` for the whole update, or
a commit reverting just the paths they dislike; never rewind history.

`<merge sha>` is the update's `update-self:` merge, not the tip the apply
landed. After a catch-up that tip (and the sha the version history records) is
the worker's catch-up merge or a commit on top of it, and reverting a catch-up
merge with `-m 1` takes out the workspace's own commits instead of the update:

```bash
git log --first-parent --merges -1 --format=%H --fixed-strings \
    --grep "update-self: merge upstream template"
```

The worker's own commits on top of that merge (a mirror edit, a re-fit, a gate
fix) are part of the update too: revert each of them first, newest first, then
the merge. They are the non-merge commits on the first-parent line from the
merge to the landed tip (the sha the version-history entry records); the
catch-up merges between them are the workspace's own work and stay:

```bash
git log --first-parent --no-merges --format=%H <merge sha>..<landed tip>
```

Land the revert with the same machinery, in ordinary merge mode (no
`--target-ref`), so it gets the same refresh, restart and health-probe motion
the update got:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py apply \
    --merge-ref <that branch>
```

Two residues to mention when they matter: the apt snapshot advanced by
`env-converge upgrade` stays advanced, and the version-history entry stays
(the revert is its own history). The full-rewind fallback is the backup
service's restore point, when Step 1 confirmed a recent one.

## Migration-required updates

Some updates cannot be applied in place, and that is a terminal verdict, not a
confirmation to wait on -- the judgment is the lead's,
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

## Which verdict ends the pass

Every pass ends with exactly one `run-status verdict`, and the Imbue Studio app
reports the workspace by it. Choose it by what happened:

- `UPDATED`: the apply landed and the workspace is healthy on the target.
- `UPDATED_WITH_REBUILD_ITEMS`: it landed, and the report left something only
  the user can finish.
- `ALREADY_CURRENT`: there was nothing to apply.
- `NEEDS_DECISION`: the update cannot keep something the user built, and the
  validated merge is kept for their answer ("Needs a decision"). Their answer
  resumes the pass, which then ends with the verdict that answer leads to
  (`resuming-after-a-decision.md`).
- `NEEDS_RECREATION`: the update cannot be applied in place
  ("Migration-required updates"), including a worker's `stuck` report that
  shows the merge cannot land without breaking the running workspace.
- `REFUSED`: the pass ran as it should and chose not to apply. Nothing is
  broken: there was no target it may take, the user declined or told you to
  stop (`stopping-when-asked.md`), or the apply rolled back and your
  best effort at a retry is spent.
- `STUCK`: a fault stopped the pass and you could not recover from it. The
  worker reported `stuck` for any other reason, a dead worker did not come
  back through `.agents/skills/launch-task/references/dead-worker-recovery.md`,
  a step the update depends on failed, the apply ended in an emergency
  (exit 3), Step 2 reported a fault, or this chat keeps stopping for a reason
  you cannot find or fix (`resuming-after-a-restart.md`). The watchdog
  records it for a lead whose chat was deleted.

`STUCK` and `REFUSED` say different things to the user, but the app reports
both, and `NEEDS_RECREATION`, as failures. `NEEDS_DECISION` is not a failure:
the pass did its job and the next move is the user's, so the app shows it as a
decision pending, with a way into this chat and no fresh update on offer. A
pass that is waiting on the user is therefore never `STUCK`, and one that broke
is never `NEEDS_DECISION`. Things that are not faults do not make a pass
`STUCK` either: an app that a pinned pass cannot reach, or a lead or worker
that stopped and came back on restart.
