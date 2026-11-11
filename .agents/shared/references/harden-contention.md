# Contention between passes over the same creation

Multiple chats can each change the same creation and each dispatch a harden
pass (`update-creation` / `heal-creation`). A pass covers every creation its
change touched, so two passes collide whenever they share any one creation.
Left uncoordinated this produces two failure shapes: two agents' harden
workers hardening the same creation at once, and a hardened branch getting
merged after the base it was verified against has moved.

This file names one other shared reference by role, without sending you to
it: the lead-proxy reference, which a lead follows to poll and answer its
worker. The skill that sent you here names its path. The two files this one
does send you to sit beside it.

Three principles drive every rule below:

- **A hardened branch is only trustworthy against the exact base it was
  verified on.** Merging it after the creation changed underneath -- or
  resolving a conflicted merge on the lead's side -- ships a combination
  nobody tested, which defeats the point of the pass.
- **The foreground always wins.** A user waiting in chat outranks a
  background pass. A foreground change never waits for an in-flight pass; it
  just makes that pass stale, and staleness is handled here.
- **A stale pass is caught up, not redone.** Its worker brings its branch up
  to the new base and verifies what moved in, so the work it already verified
  is kept. A fresh pass replaces it only when the worker is gone, or when
  no one who can receive its reports is left to lead it.

## Contents

- Before dispatch: one pass per creation (single-flight): before you open a
  pass's ticket
- Before merge: lease, freshness, conflicts: before any merge from a worker's
  branch
- Catching up a stale pass: when a check before the merge fails, or a ticket
  note names a creation the pass does not cover
- Taking over a pass whose holder is gone: detail in
  `harden-contention-takeover.md`
- Superseding a pass whose worker is gone: detail in
  `harden-contention-supersede.md`

## Before dispatch: one pass per creation (single-flight)

At most one harden pass per creation may be in flight, under either operation
-- an `update` and a `heal` pass on the same creation collide just as hard as
two updates. A pass's ticket title lists every creation it covers (`update
app:roadmap skill:sync-roadmap`), so before opening the tracking ticket, look
for a live pass on each creation yours would cover. The match also takes the
bare name, which is how a ticket opened before titles carried types names its
creation:

```bash
tk ready > /tmp/harden-inflight.txt
for CREATION in $CREATIONS; do
    grep -E -- "- (update|heal) ([^ ]+ )*($CREATION|${CREATION#*:})( |\$)" /tmp/harden-inflight.txt
done
```

- **No match** -- dispatch normally.
- **A match assigned to you** -- you already have a pass in flight; do not
  launch a second one. Note your new commits on your own ticket, as in the
  "Live" case below: the catch-up your merge-time freshness check forces
  covers them.
- **A match assigned to another agent** -- check whether its holder (the
  ticket's assignee, in `tk show <their-ticket-id>`) and its worker (its name
  is in the ticket's description) are still there:

  ```bash
  mngr list --format jsonl > /tmp/harden-agents.jsonl
  LEAD_ID=$(jq -r 'select(.name == "<worker-name>") | .labels.lead_agent // empty' /tmp/harden-agents.jsonl)
  jq -c --arg lead "$LEAD_ID" \
      'select(.name == "<holder>" or .name == "<worker-name>" or ($lead != "" and .id == $lead))
      | {name, id, state, archived_at: .labels.archived_at}' /tmp/harden-agents.jsonl
  ```

  The holder is running when its state is RUNNING or WAITING, found either by
  the ticket's name or by the id in the worker's `lead_agent` label: a chat
  renamed since it took the ticket has no record under the old name, but
  keeps its id. The worker is
  there when it has a record whose `archived_at` is absent or empty (a
  revived worker's is blanked; a STOPPED one without it crashed, and the next
  message restarts it; see
  `.agents/skills/launch-task/references/dead-worker-recovery.md`). No record,
  or one stopped on purpose (a non-empty `archived_at`), means the worker is
  gone.
  Then:
  - **Live** (holder running): do NOT dispatch a second pass. Leave a note on
    their ticket so the owner covers your change at merge time, and stop --
    your turn-end obligation is met, because the catch-up their merge-time
    freshness check forces will harden your commits too:

    ```bash
    tk add-note <their-ticket-id> "Commits <range> also change $CREATIONS; this pass is now stale. Catch it up at merge time per harden-contention.md."
    ```

    Their pass may cover fewer creations than your change touched. The
    note still names all of yours, so the catch-up widens the pass to the
    whole change rather than the half that overlapped.
  - **Orphaned** (holder gone, worker still there -- often waiting on a gate
    nobody relayed): become its holder rather than replacing it. See "Taking
    over a pass whose holder is gone".
  - **Abandoned** (holder and worker both gone): replace it with your own
    pass covering the union, keeping whatever it committed. See "Superseding
    a pass whose worker is gone".

Do not queue a second pass behind a live one. Queued passes verify obsolete
states; the live pass, caught up, covers the union instead.

## Before merge: lease, freshness, conflicts

Run these in order before any merge from a worker branch -- the merge on `done`
or a provisional milestone merge:

1. **Wait out the foreground lease (apps and services only).** If a creation in
   the pass is an app or service and another agent holds its editing lease (an
   open/in-progress `editing service <name>` ticket in `tk ready`, or `editing
   critical app <name>` for a critical app -- see `update-app`'s "One editor at
   a time"), do not merge or refresh mid-edit. Re-check about once a minute
   until the lease is released, then continue -- their edit will usually make
   your pass stale anyway, which the next check catches.

2. **Freshness check.** The pass is mergeable only if no creation in it has
   changed since the worker branched. The paths to diff are every creation's
   whole footprint, not just the files the worker touched, which is what the
   worker's scope file holds. That file lives under the worker's own `data/`
   (gitignored, and only the reports directory syncs back), so recompute it on
   your side with the same command:

   ```bash
   BASE=$(git merge-base HEAD "$WORKER_BRANCH")
   SCOPE=$(mktemp)
   uv run --no-sync app-manifest pass-scope $(printf -- '--creation %s ' $CREATIONS) --out "$SCOPE" || exit 1
   PATHS=$(jq -r '[.creations[] | .primary[], .wiring[].path, .references[].path] | unique | .[]' "$SCOPE")
   [ -n "$PATHS" ] || exit 1
   git diff --name-only "$BASE" HEAD -- $PATHS
   ```

   The guards matter: with an empty `$PATHS` the diff covers the whole
   tree, and any commit anywhere reads as a moved base. If the command fails
   (a reference a manifest names no longer exists, say), fix the manifest on
   your branch first and rerun; do not treat the failure as staleness. When
   the worker's `done` report lists paths under `References registered:`, add
   them to `$PATHS` as well -- your tree's manifest predates them. A critical
   app's merge lives in
   `update-app/references/critical-app.md` step 4, which applies this same
   check over the pass's footprint together with `system/libs/workspace_ui/`,
   `system/libs/workspace_layout/`, `system/libs/workspace_themes/`, `system/package.json`,
   `system/package-lock.json`, and every file the pass branch changes. Empty
   output means fresh: merge normally. Any output means the base moved under
   the worker: the pass is stale -- do not merge; catch it up (below).

   No shared *authored* file remains in that footprint -- a creation's
   supervisord program lives in its own drop-in rather than in the shared
   config, and the root `pyproject.toml` needs no per-creation entry -- so two
   creations built in the same tree no longer collide textually: neither
   appears in the other's diff, and neither makes the merge conflict.

   That is a claim about paths, not about correctness. The check reads the
   pass's own creations' paths, so it cannot see a change to something they
   *depend on*: a shared library under `system/libs/` (`app_manifest` alone
   backs `chat`, `terminal` and the system interface), a skill script an app
   calls or reads (`system_interface`'s `update_staleness_test.py` asserts
   against `update-self`'s `update_apply_contract.py`), or another creation's
   `app.toml`, which every registry reader joins against. Such a change breaks
   a pass this check calls fresh, and conflicts with nothing. Widening the
   footprint to cover it would not do: every shared-library commit would then
   stale every pass in flight, which is the serialization the split exists to
   remove. So read a green result as "nothing the pass's creations own moved",
   not as "this pass is still valid".

   `uv.lock` is deliberately NOT in the footprint. It is derived, every
   creation's scaffold regenerates it, and `.gitattributes` marks it
   `merge=binary` so any divergence is a hard conflict -- including it would
   make two concurrent creations collide on a generated file and force one of
   them into a catch-up. See step 3.

3. **Never resolve a conflicted hardened branch on your side.** If the merge
   itself conflicts, `git merge --abort` and catch the pass up (below): the
   worker resolves the conflict on its own branch and verifies the result, so
   the resolution lands tested. Resolving it here would ship exactly the
   unverified state the pass exists to prevent.

   One carve-out: a conflict confined to a **generated lockfile** (`uv.lock`,
   `package-lock.json`). Those are derived, not authored, and every creation's
   scaffold regenerates them, so two concurrent creations conflict there as a
   matter of course -- sending every one of them back to the worker would
   serialize exactly the work this layout exists to parallelize. Regenerate the
   lock from the merged manifest instead (`uv lock`, or `npm install
   --package-lock-only`) and continue the merge. This is the same rule
   `.gitattributes` states and `update-self`'s worker already follows; it is
   not hand-resolving, because nothing is being chosen by hand. If anything
   *outside* the lockfiles also conflicts, the carve-out does not apply --
   abort and catch the pass up.

### Provisional milestone merges

A worker can declare a **milestone** mid-pass: a commit it says is already worth
using (the lead's side is "Milestone reports: provisional merge" in the
lead-proxy reference). Merging one runs the
same three checks above, with the target pinned to the milestone's `commit:`
rather than the branch tip. A milestone that fails them is simply not merged:
the worker is still mid-pass, and its `done` is caught up as usual.

This is the **one sanctioned way not-yet-hardened work reaches the lead's
branch**. The merge commit says so (`Provisional merge of <worker> at milestone
<name>`), and it is verified only as far as the milestone's `## Tested` states.

The freshness rule composes: the provisional merge advances
`git merge-base HEAD "$WORKER_BRANCH"` to the milestone commit, so at `done` the
same check covers exactly the window since that merge, and a foreground edit
inside it makes the pass stale by the usual rule.

## Catching up a stale pass

Whoever finds the staleness at merge time -- a moved base, a conflicted merge,
or an apply refused because `HEAD` moved -- sends the pass back to its worker
to catch up. So does a note on the pass's ticket that names a creation the pass
does not cover, which the freshness check cannot see, since it reads only the
pass's own creations: read the ticket's notes (`tk show <ticket-id>`) before
you merge. **Keep the worker**: its branch, its worktree and anything it
built (a critical app's bundles) are the verified work the catch-up builds on.
Never destroy it to make room for a new pass.

Ask it with a reply naming your current `HEAD` and what moved, then re-arm the
poll exactly as for a gate (the lead-proxy reference):

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py reply \
    --task-file <TASK_FILE> -m "Catch up to $(git rev-parse HEAD) per 'Catching up to a moved base' in harden-creation.md: merge it into your branch, resolve any conflict, harden and verify what it brings in, and report done again. What moved: <the \$BASE..HEAD commits touching its creations, a conflicted path, and any notes on the ticket>. Add to the pass: <each creation a note names that the pass does not cover, as type:name>."
```

Leave out the `Add to the pass:` sentence when no note names one. When one
does, add those creations to your own `$CREATIONS` too, so the freshness check
and go-live cover them from then on, and record them where other chats read
the pass: your task file's `creations` line, which a chat taking the pass over
leads it with, and the ticket's title, which the single-flight check
matches. A ticket's title is the first `# ` line of its file:

```bash
ADDED="<each added creation, as type:name>"
sed -i "s|^creations: .*|& $ADDED|" <TASK_FILE>
sed -i "0,/^# /s|^# .*|& $ADDED|" "$TICKETS_DIR/<ticket-id>.md"
```

The worker's side is `.agents/skills/harden-worker/references/harden-creation.md`
("Catching up to a moved base"), or its own flow's catch-up section where one
exists (`update-self`'s worker has its own). It merges your `HEAD` into its
branch, so merging that branch back into yours conflicts with nothing. It
resolves every conflict on its side, regenerating lockfiles rather than
choosing sides. The commits it brings in that touch a creation in the pass get
the same hardening as the rest of the pass, and its test gate re-runs over
what the catch-up changed for the pass, not over every other chat's commits in
the window. Then it reports `done` again.

That report goes through the same checks before merge as the first one.
Usually it is fresh now. If `HEAD` moved again while the worker caught up,
catch it up again: each round covers only the commits since the last, so the
rounds shrink. A worker that cannot reconcile the new base reports `stuck` or
asks a `question`, handled as on any run.

A provisional milestone you reverted needs nothing more here: its worker
already brought the milestone's work back on its own branch when you handed it
the revert ("Rolling back a provisional merge", in the lead-proxy reference's
milestone file).

## Taking over a pass whose holder is gone

A pass whose holder stopped while its worker is still there is neither stale
nor abandoned: the worker holds verified work and is waiting for a lead, often
on a gate nobody relayed. Become that lead; do not replace the pass. Read
`harden-contention-takeover.md` when the single-flight check reports an
orphaned pass: it has how to locate the pass's task file, take its ticket and
its worker, read the last report the worker sent, and act on it.

## Superseding a pass whose worker is gone

When there is no worker left to catch up -- an abandoned pass, one stopped
after a failure the user did not override, or an orphaned pass whose reports
cannot reach you -- replace it with **one** new pass that carries whatever
work the old one left unmerged. That work is never dropped. Read
`harden-contention-supersede.md` before you destroy anything: it has how to
commit and keep the old pass's work, the destroy, what
the new pass's task names under `## Carried work`, and the two wrinkles of a
pass that had already delivered a provisional milestone merge.
