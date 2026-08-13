---
name: update-system-interface
description: Canonical flow for changing the system interface (the web workspace UI at system/apps/system_interface) -- its frontend (dockview shell, chat rendering, progress view) or backend (Flask server, agent discovery, layout ops). Use whenever the user wants to edit, fix, restyle, or add to the workspace UI / chat interface / dockview.
---

# Updating the system interface

`system/apps/system_interface` is the live web UI the user is looking at right now
(the dockview shell, the chat panels, the progress view) -- **the app that
*is* the workspace UI**. This skill is the system-interface *specialization* of
`update-app`'s "live loop first, ratify at turn-end" shape: everything
shared -- the editing lease, the demonstrative-prototype (mock) taxonomy, the
turn-end harden handoff -- lives in
[`update-app`](../update-app/SKILL.md) and the references it points at,
and this skill carries only what the system interface does *differently*.

> **Read [`update-app`](../update-app/SKILL.md) first, then come back here.**
> This skill is a set of deltas, not a standalone flow: it names only what
> differs and assumes you have the shared mechanics (lease, live loop, mock
> taxonomy, turn-end harden) from that skill. Skipping it leaves you missing
> steps this file deliberately does not repeat.

Everything different traces to one fact: **a broken build here is served
straight to the user as their entire workspace.** That forces three adjustments
to the ordinary live loop:

1. **Code isolation.** You edit an *isolated git worktree*, never the served
   tree -- a half-broken build can never reach the served UI.
2. **The isolated preview instance *is* the user's view.** For an ordinary
   app the user watches the live tab and a preview is the exception; here the
   live tab is off-limits, so a labeled preview tab is the normal, always-on way
   the user sees the change as you iterate.
3. **Safe-reveal go-live.** Merging is not enough: going live runs a
   health-checked, auto-rollback reveal script so a bad change can never take the
   UI down.

The loop stays cheap: the lead edits the worktree, builds, and refreshes the
preview in place (seconds); the expensive test + review gate runs once, in a
background worker, only after the user approves the shape.

## The hard rule

**Never edit the system-interface tree that is being served to the user.** Do
not run `Edit`/`Write` on files under `system/apps/system_interface/` in this (the
served) checkout, and do not rebuild or restart the live UI from uncommitted
edits here. Every change is made in a separate, isolated worktree, built and
previewed there, and revealed to the live tree only through the safe-reveal
script once the user has approved and a background worker has hardened it.

## Flow overview

1. **On entry:** take the editing lease and kick off worktree provisioning **in
   the background** while you read the code and pin down the change with the user.
2. **Live loop:** edit the worktree -> build -> refresh the preview in place ->
   surface to the user -> iterate. Commit before each surface, so branch `HEAD`
   always equals the last thing the user saw.
3. **On approval:** hand the approved shape to a background harden worker *on the
   same branch* (two handoff shapes), with an optional final preview of any real
   work the user hasn't seen.
4. **Go live:** freshness-check, capture the rollback point, merge, run the
   safe-reveal script, then tear everything down and release the lease.

## 1. On entry: take the lease, provision in the background, clarify the shape

**Take the editing lease first**, exactly as `update-app`'s "One editor at a
time" describes (same pre-flight, same advisory semantics, same
never-break-it-silently rule). Three deltas:

- **The service name is fixed:** `editing service system_interface`.
- **It is held for the whole pass**, not per turn -- entry through reveal or
  abandonment, including the waits for the user's feedback -- and released only at
  final teardown (Step 4) or on explicit abandonment. There is one served UI and
  one preview tab, so only one system-interface edit may be in flight at a time.
- **Breaking a stale one also means tearing down its orphaned pass:** its preview
  service and tab, its worktree, and its worker if one exists. Run the Step 4
  teardown (the shared script's `down` + the `layout.py close` loop),
  `git worktree remove`, and
  `create_worker.py destroy` for whatever the abandoned pass left behind.

**Pick a slug** `$SLUG` for the change. The branch is `mngr/update-$SLUG`; the
lead's editing worktree lives at `data/.tasks/si-live/update-$SLUG/` (gitignored,
and *separate* from the worker's runtime dir so it is never rsynced into the
worker); the worker's runtime dir is `data/.tasks/harden/update-$SLUG/`.

**Kick off provisioning in the background, then start exploring.** The one real
up-front cost is standing up a built worktree; hide it behind the exploration you
were going to do anyway. Launch this as a background task and immediately start
reading the relevant code and clarifying the change's shape with the user:

```bash
git worktree add -b "mngr/update-$SLUG" "data/.tasks/si-live/update-$SLUG" HEAD
cd "data/.tasks/si-live/update-$SLUG" && uv sync --all-packages \
  && (cd system/apps/system_interface/frontend && npm ci && npm run build)
```

**If `git worktree add -b` fails because `mngr/update-$SLUG` already exists**, an
earlier pass on this slug was abandoned without tearing down (or a worker still
holds the branch). Do *not* force past it -- that branch may carry unmerged work.
Look at what is on it (`git log --oneline HEAD..mngr/update-$SLUG`) and surface
the choice to the user: resume it, or pick a fresh `$SLUG` and leave the old
branch alone. Delete it only if the user says so. Resuming checks the existing
branch out as-is -- note the different argument shape (the branch is now the
commit-ish, not a `-b` flag value):

```bash
git worktree add "data/.tasks/si-live/update-$SLUG" "mngr/update-$SLUG"
```

That still fails if a worker or another worktree is holding the branch, which is
the case to surface rather than force past.

By the time you have an edit to show, the worktree is warm. **How rough the
first previewed pass should be scales with shape-uncertainty, not with "does it
change what the user sees":** an obvious contained change (font, color,
reposition, copy) you implement directly; a redesign / new view / non-obvious
layout starts as a deliberately rough pass for fast signal. Which
demonstrative-prototype *type* to use is the shared taxonomy in
[`interactive-delivery.md`](../../shared/references/interactive-delivery.md)
(phase 5): the embedded workspace UI **defaults to Type 1 (a janky real edit in
the worktree, shown through the real preview)**; reserve Type 2 (a detached
throwaway prototype) for a genuinely standalone new surface where real wiring is
costly and a fake conveys the idea.

## 2. The live loop: edit the worktree, refresh the preview in place

Work entirely inside `data/.tasks/si-live/update-$SLUG/` -- that is the delta;
the loop itself (including the `frontend-design` / `use-ai-integration` rules for
what you write) is `update-app`'s. The build/test mechanics for the system
interface (in-process backend tests, the `test_e2e.py` Playwright harness, `npm
run build`/`lint`/`test`) are the worker's job at harden time and are documented
in
[`type-system-interface.md`](../../shared/worker/references/type-system-interface.md);
in the live loop you only need a clean build, not the full gate.

`update-app`'s step 4, **Verify**, carries over with its own timing rule intact
(*verify before the user can see it*) -- which here means the boot check below,
and nothing after the tab is open. Two system-interface reasons make that
sharper than usual: the preview keeps real agent discovery, so driving it is
driving the user's real conversations against the real backend; and the tab you
open in the first round stays open for the whole pass, so there is no later
private window to verify in.

**First round -- boot the preview, confirm it came up, then hand it over.** Boot
it first, on its own:

```bash
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py preview \
    --slug "update-$SLUG" --work-dir "data/.tasks/si-live/update-$SLUG"
```

`preview` boots `uv run system-interface` from the worktree's already-built app
dir on a free port, with layout persistence neutered (it drops `MNGR_AGENT_ID`, so
it cannot touch the live `layout.json`) -- meaning the preview opens with the
default tab layout, not the user's. Agent discovery is kept, so the user's real
conversations render, and the whole thing is wrapped in a labeled "preview" frame
the user opens as the `si-preview` tab. It never touches the served tree. (It
refuses to boot if another pass's preview is already up rather than hijacking the
tab; surface that and coordinate.)

**Exit 0 is your verification that it works** -- the health gate is strict, and
refuses to go green unless the lifecycle stream is really feeding the instance --
so there is nothing further for you to check before showing it. On a non-zero
exit, fix the build and re-run; do not open the tab on a broken boot. It also
reports, on stderr, the instance name (`si-preview-update-$SLUG`) you address for
every refresh and teardown below.

Only once it is up, open the tab:

```bash
for L in desktop mobile; do python3 system/scripts/layout.py open --layout "$L" si-preview; done
```

**That `open` is the hand-off, not setup.** It puts the tab on the user's screen
the moment it returns -- so from here the pass is interactive: every round ends
by telling the user what changed and waiting. Do not drive the preview yourself
after this point (`update-app` step 4 above), and never tell the user to "open
si-preview" -- you already did, and they have been looking at it.

The `for L in desktop mobile` loop is the same `--layout` handling `update-app`
describes, and it applies to every `close` below too. (`refresh` is the
exception: it takes no `--layout`.)

**Re-running `preview` mid-loop is safe** -- the instance died, or you are picking
the pass back up in a later turn -- so just boot and re-open (`layout.py open`
focuses the existing tab rather than stacking a duplicate). Prefer the shared
script's `refresh` (below) for ordinary rounds anyway -- it is much faster, and
it keeps the port, so the tab stays pointed at a live instance.

**Each subsequent round -- refresh in place; the tab never goes blank.** The tab
points at the wrapper page, which never moves. Two different things are called
"refresh" here, and you often want both: the shared script's `refresh` restarts
the *server*, `layout.py refresh` reloads the *iframe*. After editing:

- **Frontend-only round:** rebuild, then reload the iframe. No process bounce
  (the inner app serves the rebuilt `static/` bundle straight from disk):

  ```bash
  (cd data/.tasks/si-live/update-$SLUG/system/apps/system_interface/frontend && npm run build)
  python3 system/scripts/layout.py refresh si-preview
  ```

- **Backend round (Python / server logic):** additionally bounce the inner app
  process on its existing port, then reload the iframe:

  ```bash
  # (rebuild first if the frontend also changed)
  python3 .agents/shared/scripts/serve_isolated_instance.py refresh --name "si-preview-update-$SLUG"
  python3 system/scripts/layout.py refresh si-preview
  ```

  That is the shared script's own `refresh`, addressed by the instance name
  `preview` printed -- there is no wrapper for it here, since it needs nothing
  from this flow but the slug. It restarts only the inner app on the same port
  and re-runs the health check; the wrapper frame and the user's tab are
  untouched. If it exits non-zero the new build did not boot -- the tab will show
  an error until you fix it and refresh again; the *live* UI is unaffected either
  way.

**Commit before each surface.** After each round you show the user, commit in the
worktree so branch `HEAD` always equals what they are looking at:

```bash
git -C data/.tasks/si-live/update-$SLUG add -A
git -C data/.tasks/si-live/update-$SLUG commit -m "wip: <what this round changed>"
```

Then get the user's reaction -- a binary keep/keep-iterating plus room for
free-form notes -- and loop until they **explicitly confirm** the shape. That
confirmation is the gate to hardening; nothing heavy runs before it.

**A test-only / no-surface change** (e.g. a test-suite fix with nothing to look
at) skips the preview entirely: edit the worktree, commit, then go straight to
the harden handoff and safe-reveal below. Code isolation is still required --
every system-interface change runs through the worktree -- but there is no shape
to preview.

The worktree and preview **persist across turns**; if the user drifts away and
never approves, you release nothing automatically (no idle timeout) -- explicit
abandonment tears everything down (Step 4 teardown) and releases the lease.

## 3. On approval: hand off to a background harden worker on the same branch

Once the user approves the shape, hand the branch to a background worker that
runs the full test + review gate. This reuses the `update-creation` orchestration
core (`type=system-interface`), with two system-interface deviations: the
worker is created **at approval, on the existing branch**, and the task frames
one of two handoff shapes.

**Free the branch first.** Git forbids the same branch checked out in two
worktrees, so before creating the worker you must release the lead's hold on
`mngr/update-$SLUG`:

```bash
# tear the live preview down and close its tab (it boots from the worktree)
python3 .agents/shared/scripts/serve_isolated_instance.py down --name "si-preview-update-$SLUG"
for L in desktop mobile; do python3 system/scripts/layout.py close --layout "$L" si-preview; done
# then remove the lead's worktree, freeing the branch for the worker
git worktree remove data/.tasks/si-live/update-$SLUG
```

Deliberately no `--force`: every build output in that worktree (`.venv/`,
`node_modules/`, `static/`, `.test_output/`) is gitignored, so a worktree whose
rounds you committed removes cleanly. If git refuses, the worktree still holds
uncommitted work -- commit it (which also restores the branch-`HEAD`-equals-what-
the-user-saw invariant) and retry. Never discard it to get past the refusal; the
branch you are about to hand the worker is the only copy.

**Create the worker on the branch.** Follow `update-creation` Steps 1-3 (open the
`update-$SLUG` tracking ticket, write the task file with `operation: update` /
`type: system-interface` frontmatter, launch, background-poll) with these
specifics:

- Launch with the **branch passthrough** so the worker checks out and *extends*
  the branch you built up, instead of branching anew from the served HEAD (which
  would lose your live commits):

  ```bash
  uv run .agents/skills/launch-task/scripts/create_worker.py launch \
      --name "update-$SLUG" --template subskill-worker \
      --runtime-dir "data/.tasks/harden/update-$SLUG/" \
      --task-file "data/.tasks/harden/update-$SLUG/task.md" \
      --branch "mngr/update-$SLUG"
  ```

  The worker re-syncs its own fresh worktree after launch, in the background,
  where nobody is waiting.

- **Task body = one of two handoff shapes:**
  - *Type 1 (janky real edit approved):* "the branch carries an approved but
    rough real edit -- implement the approved shape for real, then harden it."
  - *Harden-only (already-real-and-previewed, or committed-origin verify):* "the
    branch already carries the real, user-approved change -- verify and harden it;
    do not re-implement it."

  Per the system-interface exception in
  [`op-update.md`](../../shared/worker/references/op-update.md), there is **no
  `## Change origin` marker and no worker gate**: user approval already happened
  through your live loop. The worker implements/verifies per
  `type-system-interface.md`, runs the tests and review gates, and reports a
  plain `done` (or `question` / `stuck`). Include a `## Real scenario` section
  when a real conversation motivated the change -- name the motivating agent
  (usually your `$MNGR_AGENT_ID`) and describe in plain words what looked wrong,
  so the worker opens *that* conversation firsthand rather than reconstructing it
  from prose.

- **Terminal handling:** on `done`, go to Step 4. On `stuck` or a dead-worker
  timeout, surface to the user per
  [`worker-failure.md`](../launch-task/references/worker-failure.md) -- do not
  merge or reveal, and do not retry silently.

**Optional final preview before merge.** Keep a pre-merge preview when the worker
produced **real work the user has not seen** (the Type 1 janky -> real path: the
worker turned the approved rough edit into the real implementation) -- boot the
worker's already-built work_dir and let the user confirm the real version:

```bash
WORK_DIR=$(mngr ls --include 'name == "update-'"$SLUG"'"' --format json \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["agents"][0]["work_dir"])')
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py preview \
    --slug "update-$SLUG" --work-dir "$WORK_DIR"
```

Same hand-off rule as the first round: check the boot's exit code, *then* open --
so this is a second command, not the next line of that one. Only once it is up:

```bash
for L in desktop mobile; do python3 system/scripts/layout.py open --layout "$L" si-preview; done
```

And once it is open it is the user's to judge -- you do not drive it.

**Two things must both hold**, and the second is a real judgment, not a
formality:

1. The worker produced **real work the user has not seen**, and
2. **the user can actually observe and judge what changed.**

Only the second gate needs your judgment, because the first question a preview
seems to answer -- *does it even boot?* -- is already answered mechanically by
safe-reveal's health check and auto-rollback. What is left is *does this look
right*, and that is the only thing the user's eyes add.

So a visual or layout change always warrants the preview: the system interface
*is* their workspace, and a taste mismatch is expensive to discover after go-live.
But a fix whose effect they cannot trigger on demand -- a race, an error path, a
bug that needs setup they cannot drive from a tab -- gives them nothing to look
at, and asking them to stare at an apparently unchanged UI teaches them that
approving a preview means nothing. For those, the evidence that the fix works is
a regression test that fails before and passes after, which the harden gate
already produced; say that instead of booting a preview. This is the same
reasoning as the test-only / no-surface carve-out in Step 2, applied at merge
time.

It is likewise optional when the user already previewed a polished, real version
and the worker changed nothing they would see. If the user rejects here, do not
merge; tear the preview down and decide *with them* whether to re-brief the
worker.

## 4. Go live: freshness-check, merge, safe-reveal, tear down

With the worker `done` (and any final preview approved), merge and reveal. You
already hold the editing lease from Step 1, so no other chat's merge can
interleave.

1. **Freshness check** -- the branch is mergeable only if `system/apps/system_interface/`
   has not changed on the served branch since the worker branched:

   ```bash
   BASE=$(git merge-base HEAD "mngr/update-$SLUG")
   git diff --name-only "$BASE" HEAD -- system/apps/system_interface/
   ```

   Empty output means fresh -- continue. Any output means the pass is stale (some
   other change landed on the served tree); do **not** merge and never
   hand-resolve a conflicted merge (see
   [`harden-contention.md`](../../shared/references/harden-contention.md)).
   Re-brief the worker to rebase and re-verify, then come back.

2. **Capture the known-good revision** -- the served `HEAD`, *before* you merge.
   This is what the reveal rolls back to if the change breaks:

   ```bash
   ROLLBACK_TO=$(git rev-parse HEAD)
   ```

3. **Merge** `mngr/update-$SLUG` into the served working branch and commit the
   merge, so the tree is clean (the reveal refuses to run on a dirty tree). The
   built `static/` bundle is gitignored, so the merge brings only source and
   dependency-manifest changes; the reveal rebuilds the bundle.

4. **Reveal** with the captured revision:

   ```bash
   python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py reveal \
       --rollback-to "$ROLLBACK_TO"
   ```

   That single command owns the whole reveal as one deterministic, self-healing
   motion (you do not run `npm`/`uv`/`mngr` by hand). It classifies what changed;
   refreshes dependencies only if a manifest changed (`npm ci` / `uv tool install
   -e system/apps/system_interface --reinstall`); pre-flights a backend change on a
   throwaway port before touching the live service; rebuilds `static/` (frontend)
   and/or restarts the services agent (backend); rebuilds the user's view
   afterwards via `system/scripts/refresh_workspace_view.py` -- for a backend-only
   change too, since the restart leaves the open page rendering what it had
   already fetched, and best-effort, so it never fails a reveal that landed;
   health-checks the live service; and auto-rolls-back to `--rollback-to` on any
   failure. Interpret the exit code and report it:

   - `0` -- revealed; the live UI is updated and healthy.
   - `2` -- the change was bad and was **automatically rolled back**; the live UI
     is healthy on the previous revision, but the requested change did **not**
     land. Diagnose before retrying.
   - `3` -- **emergency**: even rollback could not restore a healthy UI. Escalate
     immediately.
   - `1` -- precondition error (e.g. a dirty tree); nothing was changed.

   Why a script and not a checklist: if the backend fails to start, the user
   loses their entire chat UI -- there is nowhere left to surface an error. The
   recover-or-revert logic must run identically every time and can never be
   skipped.

5. **Tear down and release.** After a successful reveal (or after a rejection
   where nothing was merged), tear down any remaining preview and its tab,
   destroy the worker, close the ticket, and release the lease:

   ```bash
   python3 .agents/shared/scripts/serve_isolated_instance.py down --name "si-preview-update-$SLUG"
   for L in desktop mobile; do python3 system/scripts/layout.py close --layout "$L" si-preview; done
   ```

   `down` is idempotent (a missing instance is a no-op success), so it is safe
   after a reveal, after a rejection, or to clean up a half-set-up preview. It
   only handles the *service*; the `si-preview` tab is a layout panel
   you must close yourself, or the user is left with a stale tab pointing at a
   deregistered service. Then destroy the worker per `launch-task`, close the
   `update-$SLUG` ticket, and release the editing lease with
   `tk close "$LEASE_ID" "Live edit hardened, revealed, and torn down."`. Also
   remove the lead's worktree if it still exists (`git worktree remove
   data/.tasks/si-live/update-$SLUG` -- again without `--force`, so a refusal
   surfaces uncommitted work instead of deleting it).

## Why this shape

The safety (never serve a broken build) used to mean waiting out a full harden
pass before the user could see *anything*, inverting "live first, ratify at
turn-end." Code isolation and the auto-rollback reveal keep that safety while
restoring the fast loop. Because preview boot, in-place refresh, and reveal are
deterministic, they live as sub-commands of `reveal_system_interface.py`; the
only non-deterministic part -- gating on the user's judgment -- stays with you.
