# Lead-side proxy flow: milestone reports

A conditional path of the lead-proxy flow. Read it when `await` returns a
`type: milestone` report, and again when the user rejects a milestone you
merged.

## Contents

- How a milestone reaches you
- Milestone reports: provisional merge
- Rolling back a provisional merge

## How a milestone reaches you

`await` returns **milestone** reports alongside gates and statuses: any file
under `<REPORTS_DIR>/milestones/` with no same-named entry in
`<REPORTS_DIR>/consumed/`. It prints the file like `report.md`, exits 0,
archives it under its own name in `consumed/` (the short sha in that name is
what keys a deferred merge, and what makes the worker's re-delivery of the same
file inert), and names both paths on stderr. `report.md` wins when both are
waiting; the milestone comes back on the next poll.

## Milestone reports: provisional merge

On `type: milestone` the worker is not asking for anything: it committed
something usable and carried on. The frontmatter pins the commit; the body says
what is usable, what `## Tested` covers at that commit, and what is
`## Still pending`.

**Default to merging** -- earlier use is the point. Do not merge when:

- the body says the creation is not yet runnable (a design-only milestone),
- one of the checks before the merge fails (below), or
- the user's more recent request is still in progress (the "do not interrupt
  more recent user work" rule applies -- handle the milestone once their
  request is finished).

Before merging, run the checks your skill runs before it merges a `done`,
exactly as for `done`. A harden lead's are the three in the harden-contention
reference beside this file ("Before merge: lease, freshness, conflicts"). Do
not re-run a check `## Tested` names as passing at this commit; you may run
ones it does not name.

Merge the **pinned sha, never the branch tip** (the tip keeps moving):

```bash
git merge --no-ff <commit> -m "Provisional merge of <WORKER_NAME> at milestone <name>"
```

If the merge changed a `pyproject.toml` or `uv.lock` (a new skill's `python/`
project, a dependency added to a skill or an app), bring the venv up to date
before running anything from it; `uv run --no-sync` never installs:

```bash
git diff --quiet HEAD^1 HEAD -- '*pyproject.toml' uv.lock || uv sync --all-packages --frozen
```

Then the **provisional go-live**, the minimum needed to use the thing: a skill
is on disk at `.agents/skills/<name>/` and invocable (one with a `python/`
project only after `uv sync --all-packages --frozen`, which installs its package
and any dependency the merge added into the workspace venv); an app gets its
window refreshed (a service has no window: restart its programs, as your
skill's go-live says). You do that refresh -- never hand it to the user. Your skill's end-of-pass work (post-crystallize
migration, closing the ticket) still waits for `done`.

Tell the user in one line and in non-technical language what's been updated. The
report body you just read is written for you, not them -- say what they can now
do, never what was tested, found or fixed:
  "Added a reusable skill."
  "Created an MVP app, using it now while it continues to be improved."

Whether or not you merged, the file is already in `<REPORTS_DIR>/consumed/`
under its own name -- the `await` that printed it archived it, as with every
report -- and the sha stays in that name, so a deferred milestone can be merged
later from the archive. Re-arm the poll; the worker's gates and terminal status
are still coming.

## Rolling back a provisional merge

```bash
git revert -m 1 <provisional-merge-commit>
git rev-parse HEAD
```

A revert is a **rejection of that milestone**: message the worker with why, as a
de facto "no with notes" (the merge commit's subject names the worker and
milestone, so the target is easy to find), and hand it the revert, the commit
the second command printed:

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py reply \
    --task-file <TASK_FILE> -m "<why the milestone was reverted, in the user's voice> I reverted it as <the revert commit>: bring it back on your branch per 'A milestone the lead reverted' in the worker-reporting reference, then fix it."
```

The reverted commits remain ancestors of your `HEAD`, so a later merge from
that branch would **silently omit** them. The worker brings them back on its
own branch, as a revert of your revert, so they reach yours again only with
its next merged report, fixed. Never reinstate them on your branch yourself:
that puts the work the user rejected back in front of them.

A pass is superseded instead only once its worker is gone, and its new task
then names the revert (the harden-contention reference, "Superseding a pass
whose worker is gone").
