# Contention between passes over the same creation

Multiple chats can each change the same creation and each dispatch a harden
pass (`update-creation` / `heal-creation`). A pass covers every creation its
change touched, so two passes collide whenever they share any one creation.
Left uncoordinated this produces two failure shapes: two agents' harden
workers hardening the same creation at once, and a hardened branch getting
merged after the base it was verified against has moved.

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

## Before dispatch: one pass per creation (single-flight)

At most one harden pass per creation may be in flight, in either flow -- an
`update` and a `heal` pass on the same creation collide just as hard as two
updates. A pass's ticket title lists every creation it covers (`update
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
  launch a sibling. Note your new commits on your own ticket, as in the
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
   uv run app-manifest pass-scope $(printf -- '--creation %s ' $CREATIONS) --out "$SCOPE" || exit 1
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
using (`worker-reporting.md` for the worker's side, `lead-proxy.md`'s
"Milestone reports: provisional merge" for the lead's). Merging one runs the
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
poll exactly as for a gate (`lead-proxy.md`):

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py reply \
    --task-file <TASK_FILE> -m "Catch up to $(git rev-parse HEAD) per 'Catching up to a moved base' in harden-creation.md: merge it into your branch, resolve any conflict, harden and verify what it brings in, and report done again. What moved: <the \$BASE..HEAD commits touching its creations, a conflicted path, and any notes on the ticket>. Add to the pass: <each creation a note names that the pass does not cover, as type:name>."
```

Leave out the `Add to the pass:` sentence when no note names one. When one
does, add those creations to your own `$CREATIONS` too, so the freshness check
and go-live cover them from then on, and record them where other chats read
the pass: your task file's `creations` line, which a chat taking the pass over
leads it with, and the ticket's title, which the single-flight check above
matches. A ticket's title is the first `# ` line of its file:

```bash
ADDED="<each added creation, as type:name>"
sed -i "s|^creations: .*|& $ADDED|" <TASK_FILE>
sed -i "0,/^# /s|^# .*|& $ADDED|" "$TICKETS_DIR/<ticket-id>.md"
```

The worker's side is `.agents/shared/worker/references/harden-creation.md`
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
the revert (`lead-proxy.md`, "Rolling back a provisional merge").

## Taking over a pass whose holder is gone

A pass whose holder stopped while its worker is still there is neither stale
nor abandoned: the worker holds verified work and is waiting for a lead, often
on a gate nobody relayed. Become that lead; do not replace the pass.

1. Its task file is the one the calling skill writes:
   `data/.tasks/harden/<worker-name>/task.md` for an update or heal pass,
   `data/.tasks/update-self/task.md` for update-self,
   `data/.tasks/migrate-workspace/task.md` for migrate-workspace. Its
   `lead_work_dir` must be your own checkout (`$MNGR_AGENT_WORK_DIR`; the chats
   of one workspace share it), since the worker delivers every report there.
   If it names another checkout, you cannot receive its reports: supersede the
   pass instead (below). An update or heal pass's `creations` are the
   `$CREATIONS` you lead it with.
2. Move the ticket to yourself, and say why on it:

   ```bash
   tk assign <their-ticket-id>
   ```

   ```bash
   tk add-note <their-ticket-id> "Holder <holder> is gone; taken over by $MNGR_AGENT_NAME. Commits <range> also change $CREATIONS."
   ```

   Then label the worker as yours. The `lead_agent` label is how the check
   above finds you once you are renamed, and how the user's chat list files
   the worker under the chat leading it:

   ```bash
   mngr label <worker-name> -l lead_agent=$MNGR_AGENT_ID
   ```
3. Find what the worker last said. A report waiting at the task file's
   `finish_report_path` is unread, and the poll in step 4 hands it to you.
   Otherwise its last report is the newest file in `consumed/` beside it that
   arrived after this worker was created (an earlier pass under the same name
   leaves its reports there too):

   ```bash
   mngr list --format jsonl > /tmp/harden-agents.jsonl || exit 2
   CREATED=$(jq -r 'select(.name == "<worker-name>") | .create_time' /tmp/harden-agents.jsonl)
   [ -n "$CREATED" ] || exit 1
   find "$(dirname <finish_report_path>)/consumed" -maxdepth 1 -type f -newermt "$CREATED" \
       -printf '%T@ %p\n' | sort -n | sed -n '$s/^[^ ]* //p'
   ```

   It prints that report's path. Exit 1 means no agent has the worker's
   name, so the worker is gone: supersede the pass instead (below). Exit 2
   means the agent list could not be read, which says nothing about the
   worker: run the block again. No
   output means the worker has reported nothing yet. A worker that is
   RUNNING was answered (a `done` it is catching up on, say), and its next
   report reaches you through the poll in step 4. Otherwise the holder read
   that report but stopped before acting on it, so act on it now, by what it
   is:
   - A `gate` or `question` was never answered. Handle it as `lead-proxy.md`
     says ("Deciding: answer gate yourself vs. escalate"). A
     `final-creation` gate goes to the user, with a sentence saying it comes
     from an earlier check of this creation that has been waiting on them.
   - A `done` was never merged: run the calling skill's merge for it now,
     its checks before merge first ("Before merge" above). They catch the
     pass up when your commits moved its base.
   - A `no-update-needed` or `stuck` is handled as `lead-proxy.md`'s
     "Terminal status" says.

   A worker that crashed restarts with your reply, or with `continue` when
   there is nothing to answer.
4. Unless step 3 ended the pass, arm the poll (`lead-proxy.md`) and drive the
   pass to its end as its lead.
   Your own commits need nothing extra: the freshness check at merge time
   finds them, and the pass is caught up, adding any of your creations it
   does not cover (the note in step 2 names them).

## Superseding a pass whose worker is gone

When there is no worker left to catch up -- an abandoned pass, or one stopped
after a failure (a `stuck` on the catch-up itself included) whose user did not
override it -- replace it with **one** new pass. A worker the user's override
resumes is kept (`lead-proxy.md`, "Resuming after the user overrides a
failure"). An orphaned pass whose task file names another checkout as
`lead_work_dir` is replaced the same way although its worker is still there:
its reports can never reach you, so no one here can lead it.

A pass that is gone can still have left work no one merged: commits on its
branch or a sub-worker's, a fix it finished before its holder stopped
answering. That work is never dropped. The new pass carries it:

1. **Commit what its worktrees hold.** A destroy loses uncommitted changes
   with the worktree. For the worker and each sub-worker, read `work_dir` from
   its `mngr list --format jsonl` record and run `git -C <work_dir> status
   --porcelain`; commit anything there on that worktree's branch, per steps
   1-3 of "Last resort: manual worktree salvage" in
   `.agents/skills/launch-task/references/dead-worker-recovery.md`.
2. **Destroy it, keeping its commits.**

   ```bash
   uv run .agents/skills/launch-task/scripts/create_worker.py destroy --name <worker-name> --delete-branches
   ```

   Before destroying anything, the launcher checks every branch it would
   delete, the pass's and its sub-workers'. When one holds commits that
   neither your `HEAD` nor any other branch has, it refuses and prints, for
   each, the `git branch superseded/<name> <branch>` command that keeps it
   under a name the new launch will not need. Run those commands, then the
   destroy again. The branches themselves still have to go, because the
   launch cannot reuse the worker's name while they exist. Never pass
   `--discard-unmerged` unless the user has said that work is not wanted.

   ```bash
   tk close <old-ticket-id> "Superseded -- worker gone; re-dispatched covering the union."
   ```
3. **Dispatch the new pass with that work in scope**, through the normal flow
   (Steps 1-3 of the calling skill). Its scope covers **everything since the
   last hardened merge**: at minimum the `$BASE..HEAD` commits touching its
   creations, whatever any notes on the old ticket describe (and any creation
   the notes name that the old pass did not cover, which the new pass covers
   too), and each `superseded/` branch you kept. Name those in the task body under a
   `## Carried work` heading, one per line with its commits (`git log
   --oneline HEAD..<branch>`). The worker merges them into its own branch
   before hardening and treats them as part of the change
   (`harden-creation.md`, "Carried work from a superseded pass"). They are
   merged there and not into your branch, because nothing has verified them
   yet. One superseding pass validates the union of all pending changes
   together -- which is the only combination that will actually run. A worker
   that follows its own guide rather than `harden-creation.md` (update-self's,
   migrate-workspace's) has no such step, so its new pass carries nothing:
   leave each kept branch for the user to decide on, and say so in step 4.
4. **Tell the user** what was kept, in a sentence about the work itself (from
   the commit subjects), and that the new pass is finishing it.
5. **Once you merge the new pass's `done`**, delete each branch you named
   under `## Carried work` with `git branch -d superseded/<name>`. The
   lowercase `-d` refuses a branch your `HEAD` does not contain, which would
   mean the worker never merged it: ask it why rather than forcing the delete.
   A kept branch you left for the user stays until they decide.

Two wrinkles when the pass had already delivered a provisional milestone
merge:

- **A provisionally merged milestone survives.** It is already a commit on your
  own branch, so deleting the worker branch does not take it back out. The
  hardening done after that milestone is unmerged work like any other, and the
  new pass carries it.
- **A reverted milestone is carried by its revert.** If you rolled a
  provisional merge back with `git revert -m 1 <merge-commit>`, the reverted
  commits are still ancestors of your `HEAD`, so carrying the branch cannot
  bring them back, and `git log HEAD..<branch>` does not list them. The old
  worker may already have brought them back, as a revert of your revert: on
  its branch, which carrying the branch then carries, or on yours, through a
  later report of its you merged. When it had not, name the revert commit
  under `## Carried work` as well, and the new worker reverts it on its own
  branch (`harden-creation.md`, "Carried work from a superseded pass"). Never
  reinstate it on your branch yourself. Check your `HEAD` together with each
  kept branch (with none kept, `HEAD` alone):

  ```bash
  git log --oneline HEAD superseded/<name> --grep "This reverts commit <revert-commit>"
  ```

  Empty output means the old worker had not brought it back.
