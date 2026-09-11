# The careful flow: changing a critical app

You are here because the app's `app.toml` says `critical = true` (the shell,
the chat, the terminal, and any user app that declares it), or because the
change is under `system/libs/workspace_ui/`, which rebuilds both the shell's and
the chat's bundles. A broken build of one of these is served straight to the
user as their workspace, or as the surface they would use to fix it. That forces
three adjustments to `update-app`'s ordinary live loop, and nothing else changes:
the editing lease, the demonstrative-prototype taxonomy, and the turn-end harden
handoff are all `update-app`'s and the references it points at.

1. **Code isolation.** You edit an *isolated git worktree*, never the served
   tree. A half-broken build can never reach the served app.
2. **The preview is the user's view.** For an ordinary app the user watches the
   live tab and a preview is the exception; here the live tab is off-limits, so
   the labeled `<name>-preview` tab is the normal, always-on way the user sees
   the change as you iterate. Every critical app previews, the terminal included.
3. **Go-live is the atomic apply, after the harden pass.** Merging is not
   enough: the change lands through the update apply (pre-flight,
   health-checked, auto-rollback), and only once a background worker has
   hardened it. The apply keeps its rollback point, and the shell offers it back
   to the user as a notice they alone close.

The loop stays cheap: you edit the worktree, build, and refresh the preview in
place (seconds); the expensive test and review gate runs once, in a background
worker, only after the user approves the shape.

## The hard rule

**Never edit a critical app's tree in the served checkout.** Do not run
`Edit`/`Write` on files under its `system/apps/<package>/` (or under
`system/libs/workspace_ui/`) here, and do not rebuild or restart the live app
from uncommitted edits. Every change is made in the worktree, built and
previewed there, and landed on the live tree only through the apply once the
user has approved it and the worker has hardened it. (A heal of a critical app
is the one exception, and `heal-creation` says what it costs.)

## 1. On entry: take the lease, provision in the background, clarify the shape

**Take the editing lease first**, exactly as `update-app`'s "One editor at a
time" describes. Three deltas:

- **The lease name is fixed:** `editing critical apps`. One lease covers every
  critical app, because one apply lands them all and one preview tab per app is
  all the workspace can show.
- **It is held for the whole pass**, not per turn: entry through go-live or
  abandonment, including the waits for the user's feedback. Release it only at
  final teardown (step 4) or on explicit abandonment.
- **Breaking a stale one means tearing down its orphaned pass too:** its
  preview and tab, its worktree, and its worker if one exists. Run the step 4
  teardown for whatever the abandoned pass left behind. A stale lease is broken
  only on the user's call, never silently.

**Check for an open notice.** If the app's tabs still carry the "recently
updated" notice from an earlier apply (`GET /api/updates/pending` on the shell
answers a record naming the app), the previous update is unconfirmed. Proceed,
but tell the user so, and that this pass's apply will replace that rollback
point.

**Pick a slug** `$SLUG` for the change. The branch is `mngr/update-$SLUG`; the
worktree lives at `data/.tasks/critical-live/update-$SLUG/` (gitignored, and
separate from the worker's runtime dir so it is never rsynced into the worker);
the worker's runtime dir is `data/.tasks/harden/update-$SLUG/`.

**Kick off provisioning in the background, then start exploring.** The one real
up-front cost is a built worktree; hide it behind the reading you were going to
do anyway. Both frontends build at the npm workspace root, so a change to one
bundle or to the shared library gets both:

```bash
git worktree add -b "mngr/update-$SLUG" "data/.tasks/critical-live/update-$SLUG" HEAD
cd "data/.tasks/critical-live/update-$SLUG" && uv sync --all-packages \
  && (cd system && npm ci && npm run build)
```

**If `git worktree add -b` fails because `mngr/update-$SLUG` already exists**, an
earlier pass on this slug was abandoned without teardown, or a worker still
holds the branch. Do not force past it. Look at what is on it (`git log
--oneline HEAD..mngr/update-$SLUG`) and surface the choice to the user: resume
it (`git worktree add "data/.tasks/critical-live/update-$SLUG"
"mngr/update-$SLUG"`), or pick a fresh `$SLUG`. Delete it only if the user says
so.

How rough the first previewed pass should be scales with shape-uncertainty, not
with whether it changes what the user sees: an obvious contained change (font,
color, copy) you implement directly; a redesign or a non-obvious layout starts
as a deliberately rough pass for fast signal. The prototype *type* is the
shared taxonomy in `interactive-delivery.md` phase 5: a critical app defaults to
Type 1 (a janky real edit in the worktree, shown through the real preview).

## 2. The live loop: edit the worktree, refresh the preview in place

Work entirely inside the worktree. The loop itself, including the
`frontend-design` and `use-ai-integration` rules for what you write, is
`update-app`'s; the build and test mechanics for a critical app are the
worker's job at harden time (`type-app.md`, "Critical apps"). In the live loop
you need a clean build, not the full gate.

`update-app`'s step 4, **Verify**, carries over with its timing rule intact:
*verify before the user can see it*, which here means the boot check below and
nothing after the tab is open. The preview reads real state (the chat preview
follows the real agents, the shell preview lists the real apps), so driving it
is driving the user's real workspace; and the tab you open in the first round
stays open for the whole pass.

**First round: boot the preview, confirm it came up, then hand it over.** Boot
it first, on its own:

```bash
uv run python3 .agents/skills/update-app/scripts/preview_app.py up \
    --app <name> --worktree "data/.tasks/critical-live/update-$SLUG" \
    [--with <sibling>]... [--instance-key <key>]
```

The manifest's `[preview]` table says what boots: the shell as a read-only
preview over a seeded copy of the live state directory and a copied registry;
the chat as a secondary chat following the real agents and reading the real
accounts, opened on the conversation you pass as `--instance-key` (your own
`$MNGR_AGENT_ID`, usually); the terminal on two free ports over a copy of its
store. A `workspace_ui` change previews as the shell with the chat:
`--app system_interface --with chat --instance-key $MNGR_AGENT_ID`, and the
preview shell's copied registry points at that secondary chat. Exit 0 means it
came up healthy; on a non-zero exit, fix the build and re-run, and do not open
the tab on a broken boot. It refuses to boot if another pass's preview of the
same app is up rather than hijacking the tab; surface that and coordinate.

Only once it is up, open the tab:

```bash
python3 system/scripts/layout.py open <name>-preview
```

**That `open` is the hand-off, not setup.** It puts the tab on the user's screen
the moment it returns, so from here the pass is interactive: every round ends by
telling the user what changed and waiting. Do not drive the preview yourself
after this point, and never tell the user to open a tab you already opened.

What to point the user at: for the chat, the conversation the preview opened on
(the one that motivated the change, when one did; sends from the preview are
real, so a change about what happens when a message is sent can be tried for
real); for the shell, their real projects and tabs, with the previewed app in
place of the live one and every live app's instances beside it; for the
terminal, a session the preview creates (a real tmux session).

**If the `open` was refused** (no connected client: `has no client to apply it
(HTTP 412)`), the hand-off did not happen. Drive the preview privately
(Playwright against its port) and surface a screenshot with
`show-files-in-chat`, saying plainly that they are looking at a still image
rather than the surface. Keep trying the `open`; the moment one lands, that is
the delivered preview. Approval given on a screenshot is not approval of the
live surface; name that gap when you report the pass.

**Each subsequent round: refresh in place; the tab never goes blank.** The tab
points at the wrapper page, which never moves. The script's `refresh` restarts
the *process*; `layout.py refresh` reloads the *iframe*. After editing:

- **Frontend-only round:** rebuild at the npm root, then reload the iframe:

  ```bash
  (cd "data/.tasks/critical-live/update-$SLUG/system" && npm run build)
  python3 system/scripts/layout.py refresh <name>-preview
  ```

- **Backend round:** additionally re-boot the preview's process on its existing
  ports, then reload the iframe:

  ```bash
  uv run python3 .agents/skills/update-app/scripts/preview_app.py refresh --app <name>
  python3 system/scripts/layout.py refresh <name>-preview
  ```

  If `refresh` exits non-zero the new build did not boot: the tab shows an error
  until you fix it and refresh again, and the live app is unaffected either way.

**Commit before each surface**, so branch `HEAD` always equals what the user is
looking at:

```bash
git -C "data/.tasks/critical-live/update-$SLUG" add -A
git -C "data/.tasks/critical-live/update-$SLUG" commit -m "wip: <what this round changed>"
```

Then get the user's reaction and loop until they **explicitly confirm** the
shape. That confirmation is the gate to hardening; nothing heavy runs before it.

**A test-only or no-surface change** skips the preview: edit the worktree,
commit, then go straight to the handoff and apply below. Code isolation still
holds; there is just no shape to preview.

The preview and its worktree **persist across turns**; if the user drifts away
and never approves, nothing is released automatically. Explicit abandonment
tears everything down (the step 4 teardown) and releases the lease.

## 3. On approval: hand off to a background harden worker on the same branch

Once the user approves the shape, hand the branch to a background worker that
runs the full test and review gate. This reuses `update-creation`'s
orchestration core (`type: app`), with two deviations: the worker is created
**at approval, on the existing branch**, and the task frames one of two handoff
shapes.

**Free the branch first.** Git forbids the same branch checked out in two
worktrees, so before creating the worker, release your hold on it:

```bash
uv run python3 .agents/skills/update-app/scripts/preview_app.py down --app <name>
python3 system/scripts/layout.py close <name>-preview
git worktree remove "data/.tasks/critical-live/update-$SLUG"
```

Deliberately no `--force`: every build output in that worktree is gitignored,
so a worktree whose rounds you committed removes cleanly. If git refuses, the
worktree holds uncommitted work; commit it and retry. Never discard it: the
branch you are about to hand the worker is the only copy.

**Create the worker on the branch.** Follow `update-creation` steps 1-3 (the
tracking ticket, the task file with `operation: update` / `type: app`
frontmatter, launch, background-poll) with these specifics:

- Launch with the **branch passthrough**, so the worker checks out and extends
  the branch you built up instead of branching anew from the served HEAD:

  ```bash
  uv run .agents/skills/launch-task/scripts/create_worker.py launch \
      --name "update-$SLUG" --template worker \
      --runtime-dir "data/.tasks/harden/update-$SLUG/" \
      --task-file "data/.tasks/harden/update-$SLUG/task.md" \
      --branch "mngr/update-$SLUG"
  ```

- **Task body: one of two handoff shapes**, named as such so the worker knows
  which (`op-update.md`, the critical-app handoff):
  - *Implement for real, then harden:* the branch carries an approved but
    deliberately rough edit.
  - *Harden only:* the branch already carries the real, user-approved change;
    verify and harden it, do not re-implement it.

  There is **no `## Change origin` marker and no worker gate**: the user already
  approved the shape in your live loop. Name the app (`type-app.md`'s
  "Critical apps" section tells the worker to build both bundles at the npm root
  and report both paths). Include a `## Real scenario` section when a real
  conversation motivated the change, naming the motivating agent (usually your
  own `$MNGR_AGENT_ID`) and what looked wrong, so the worker opens *that*
  conversation rather than reconstructing it from prose.

- **Terminal handling:** on `done`, go to step 4. On `stuck` or a dead-worker
  timeout, surface to the user per `launch-task`'s `worker-failure.md`; do not
  merge or apply, and do not retry silently.

**Optional final preview before merge.** When the worker produced **real work
the user has not seen** (the implement-for-real shape) and the user can
actually observe what changed, boot the worker's already-built work_dir and let
them confirm the real version, with the same boot-then-open rule as the first
round:

```bash
WORK_DIR=$(mngr ls --include "name == \"update-$SLUG\"" --format json \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["agents"][0]["work_dir"])')
uv run python3 .agents/skills/update-app/scripts/preview_app.py up \
    --app <name> --worktree "$WORK_DIR" [--with <sibling>]... [--instance-key <key>]
python3 system/scripts/layout.py open <name>-preview
```

A fix whose effect the user cannot trigger on demand gives them nothing to look
at; for those, the evidence is the regression test the harden gate produced, so
say that instead of booting a preview. If the user rejects here, do not merge;
tear the preview down and decide with them whether to re-brief the worker.

## 4. Go live: freshness-check, apply, tear down

With the worker `done` (and any final preview approved), land the change. You
hold the editing lease from step 1, so no other chat's merge or apply can
interleave.

1. **Freshness check.** The branch is mergeable only if nothing the two bundles
   are built from has changed on the served branch since the worker branched:

   ```bash
   BASE=$(git merge-base HEAD "mngr/update-$SLUG")
   git diff --name-only "$BASE" HEAD -- system/apps/<package>/ system/libs/workspace_ui/ \
       system/package.json system/package-lock.json
   ```

   Empty output means fresh: continue. Any output means the pass is stale; do
   **not** merge and never hand-resolve a conflicted merge (see
   `harden-contention.md`). Re-brief the worker to rebase and re-verify.

2. **Apply.** Run the general update apply, the same script `update-self` lands
   releases with, pointing it at the pass branch and at the bundles the user
   last previewed (the worker's work_dir after a final preview, otherwise your
   own worktree). Resolve the worker's in the same invocation, since each bash
   call starts a fresh shell:

   ```bash
   WORK_DIR=$(mngr ls --include "name == \"update-$SLUG\"" --format json \
       | python3 -c 'import sys, json; print(json.load(sys.stdin)["agents"][0]["work_dir"])')
   python3 .agents/skills/update-self/scripts/update_self.py apply \
       --merge-ref "mngr/update-$SLUG" \
       --worker-bundle "system_interface=$WORK_DIR/system/apps/system_interface/imbue/system_interface/static" \
       --worker-bundle "chat=$WORK_DIR/system/apps/chat/imbue/chat/static" \
       --keep-rollback-point
   ```

   That one command owns the whole go-live as one deterministic, self-healing
   motion: it merges the branch, classifies what changed, snapshots the built
   bundles and the affected environments, installs the previewed bundles (a live
   build is the fallback when a path does not resolve or was built from another
   source), pre-flights a backend change on a throwaway port, restarts the
   services agent, waits for the live shell and every critical instance app to
   *settle* healthy, refreshes every open view, and **auto-rolls-back the entire
   merge on any failure**. `--keep-rollback-point` keeps the snapshots and a
   record of what it touched afterwards, which is what the notice below offers
   back; any later apply replaces them. Interpret the exit code and report it,
   per `update-self`'s `apply-outcomes.md`:

   - `0`: applied; the live app is updated and healthy.
   - `2`: the change was bad and was **automatically rolled back**; the live
     app is healthy on the previous revision and the change did not land.
     Diagnose before retrying; a rolled-back merge cannot be re-landed by
     re-running the apply, so the retry is a fresh pass off the current `HEAD`.
   - `3`: **emergency**: even rollback could not restore a healthy workspace.
     Escalate immediately, with the snapshot paths the stderr names.
   - `1`: precondition error (a dirty tree, another apply in flight, a
     conflicted merge); nothing was changed.

3. **The notice is the user's.** After a successful apply, every tab of a
   critical app whose program or bundle changed carries a band (a top banner
   for the shell itself): recently updated, with "Roll back" and "Everything
   seems good". Tell the user it is there and what it does. **Never confirm or
   roll back on the user's behalf.** Only a person closes it: confirming
   discards the kept snapshots, rolling back restores them and restarts only the
   touched programs, and the outcome shows in the notice (and stays in its
   record, `data/.state/update-apply/last-good.json`, until they close it; a
   rollback started from the notice logs to `rollback-last.log` beside it). If
   they roll back, the branch and the worker's report are the retry's input.

4. **Tear down and release.** Whatever the exit code, and after a rejection
   where nothing was merged, tear down the preview and its tab, destroy the
   worker, close the ticket, and release the lease:

   ```bash
   uv run python3 .agents/skills/update-app/scripts/preview_app.py down --app <name>
   python3 system/scripts/layout.py close <name>-preview
   ```

   `down` is idempotent and tears down the siblings it booted; the tab is a
   layout panel you close yourself. Then destroy the worker per `launch-task`
   (after a failed apply, keep it until the diagnosis is done), close the
   `update-$SLUG` ticket, remove the worktree if it still exists (again without
   `--force`), and release the lease with `tk close "$LEASE_ID" "Live edit
   hardened, applied, and torn down."`.

## Why this shape

Caution scales with the recovery path, not the surface. The shell is the
recovery surface for a broken app; the terminal handover is the floor for a
broken shell, never something this flow leans on. Code isolation and the
auto-rollback apply keep the safety while restoring the fast loop, and the kept
rollback point with its notice gives the user the last word after go-live.
