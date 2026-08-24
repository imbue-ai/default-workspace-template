# Contention between passes over the same creation

Multiple chats can each change the same creation and each dispatch a harden
pass (`update-creation` / `heal-creation`). Left uncoordinated this produces
two failure shapes: two agents' harden workers colliding (the worker name,
branch, and runtime dir are all derived from the creation name), and a
hardened branch getting merged after the base it was verified against has
moved.

Two principles drive every rule below:

- **A hardened branch is only trustworthy against the exact base it was
  verified on.** Merging it after the creation changed underneath -- or
  hand-resolving a conflicted merge -- ships a combination nobody tested,
  which defeats the point of the pass.
- **The foreground always wins.** A user waiting in chat outranks a
  background pass, and re-running a pass is cheap (a committed-origin worker
  often produces no commits of its own). A foreground change never waits for
  an in-flight pass; it just makes that pass stale, and staleness is handled
  here.

## Before dispatch: one pass per creation (single-flight)

At most one harden pass per creation may be in flight, in either flow -- an
`update` and a `heal` pass on the same creation collide just as hard as two
updates. Before opening the tracking ticket, look for a live pass:

```bash
tk ready > /tmp/harden-inflight.txt
grep -E "(update|heal) $TARGET" /tmp/harden-inflight.txt
```

- **No match** -- dispatch normally.
- **A match assigned to you** -- you already have a pass in flight; supersede
  it (below) rather than launching a sibling.
- **A match assigned to another agent** -- check whether it is actually
  live: the worker session responds to the liveness probe in
  `lead-proxy.md` (`tmux capture-pane -t minds-<worker-name>:claude -p -S -20`),
  or the ticket has recent notes/reports. Then:
  - **Live**: do NOT dispatch a second pass. Leave a note on their ticket so
    the owner coalesces your change at merge time, and stop -- your turn-end
    obligation is met, because the superseding pass their merge-time
    freshness check forces will cover your commits too:

    ```bash
    tk add-note <their-ticket-id> "Commits <range> also change $TARGET; this pass is now stale. Coalesce at merge time per harden-contention.md."
    ```
  - **Abandoned** (worker session gone, no report, holder agent not
    running): take it over. Destroy the worker, delete its branch, close
    their ticket with a note saying you superseded it, then dispatch your own
    pass covering the union (see "Superseding a stale pass").

Do not queue a second pass behind a live one. Queued passes verify obsolete
states; the newest pass always covers the union instead.

## Before merge (on `done`): lease, freshness, conflicts

Run these in order before `git merge`:

1. **Wait out the foreground lease (apps and services only).** If the creation is
   an app or service and another agent holds its editing lease (an open/in-progress
   `editing service <name>` ticket in `tk ready` -- see `update-app`'s
   "One editor at a time"), do not merge or refresh mid-edit. Re-check about
   once a minute until the lease is released, then continue -- their edit
   will usually make your pass stale anyway, which the next check catches.

2. **Freshness check.** The pass is mergeable only if the creation has not
   changed since the worker branched:

   ```bash
   BASE=$(git merge-base HEAD "$WORKER_BRANCH")
   git diff --name-only "$BASE" HEAD -- <CREATION_PATHS>
   ```

   `<CREATION_PATHS>` is the creation's whole footprint, not just the files
   the worker touched: for an app,
   `system/apps/<package>/ system/supervisord.conf.d/<name>.conf`
   (a standalone service likewise, under `system/services/<package>/`);
   for a skill, `.agents/skills/<name>/`; for a shared script or reference,
   its path; for the system interface, `system/apps/system_interface/` (that
   creation's merge lives in `update-system-interface` Step 4, which applies
   this same check). Empty output means fresh: merge normally. Any output
   means the base moved under the worker: the pass is stale -- do not merge;
   supersede it (below).

   Every path in a creation's footprint belongs to that creation alone -- its
   supervisord program lives in its own drop-in rather than in the shared
   config, and the root `pyproject.toml` needs no per-creation entry. So a
   second creation being built in the same tree cannot make this pass look
   stale, and cannot make the merge conflict.

   `uv.lock` is deliberately NOT in the footprint. It is derived, every
   creation's scaffold regenerates it, and `.gitattributes` marks it
   `merge=binary` so any divergence is a hard conflict -- including it would
   make two concurrent creations collide on a generated file and force one of
   them into a full re-harden. See step 3.

3. **Never hand-resolve a conflicted hardened branch.** If the merge itself
   conflicts, `git merge --abort` and treat the pass as stale. Resolving the
   conflict by hand would reintroduce exactly the unverified state the pass
   exists to prevent.

   One carve-out: a conflict confined to a **generated lockfile** (`uv.lock`,
   `package-lock.json`). Those are derived, not authored, and every creation's
   scaffold regenerates them, so two concurrent creations conflict there as a
   matter of course -- treating that as a stale pass would serialize exactly
   the work this layout exists to parallelize. Regenerate the lock from the
   merged manifest instead (`uv lock`, or `npm install --package-lock-only`)
   and continue the merge. This is the same rule `.gitattributes` states and
   `update-self`'s worker already follows; it is not hand-resolving, because
   nothing is being chosen by hand. If anything *outside* the lockfiles also
   conflicts, the carve-out does not apply -- abort and supersede.

## Superseding a stale pass (coalescing)

Whoever finds the staleness -- the pass owner at merge time, or the agent
taking over an abandoned pass -- replaces it with **one** new pass:

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py destroy --name <worker-name>
git branch -D <worker-branch>
tk close <old-ticket-id> "Superseded -- base moved under the pass; re-dispatched covering the union."
```

Deleting the branch is deliberate: its verification ran against a base that
no longer exists, so nothing on it is trustworthy to keep. Then dispatch a
fresh pass through the normal flow (Steps 1-3 of the calling skill) whose
scope covers **everything since the last hardened merge**: at minimum the
`$BASE..HEAD` commits touching the creation, plus whatever any notes on the
old ticket describe. One superseding pass validates the union of all pending
changes together -- which is the only combination that will actually run.
