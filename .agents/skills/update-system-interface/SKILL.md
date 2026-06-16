---
name: update-system-interface
description: Canonical flow for changing the system interface (the web workspace UI at apps/system_interface) -- its frontend (dockview shell, chat rendering, progress view) or backend (FastAPI server, agent discovery, layout ops). Use whenever the user wants to edit, fix, restyle, or add to the workspace UI / chat interface / dockview.
---

# Updating the system interface

`apps/system_interface` is the live web UI the user is looking at right now
(the dockview shell, the chat panels, the progress view). A broken build here is
served straight to the user, so you never edit the served copy directly: you
make every change in an **isolated worktree clone**, verify it builds and passes
there, and only merge it back into the served tree once it's known-good. This
skill is the single canonical path for that.

## The hard rule

**Never edit the system-interface tree that is being served to the user.** Do
not run `Edit`/`Write` on files under `apps/system_interface/` in this (the
served) checkout, and do not rebuild or restart the live UI from uncommitted
edits here. Every change is made in a separate, isolated clone of the source,
built and tested there, and merged back only after it passes. The only things
you do to the served tree are committing the merge and running the reveal
command at the end of this skill.

That isolated clone is a `launch-task` worker: it runs in its own git worktree
with its own copy of the source, so a half-broken build can never reach what the
user is looking at. The worker is just the mechanism for getting that safe,
separate place to work.

## Flow overview

1. **Delegate** the change to a worker via the `launch-task` skill. The worker
   follows the bundled `update-system-interface-worker` sub-skill, which owns all
   the detail of how to build, test, and verify the change in isolation.
2. The **worker** implements + builds + tests it on its own branch (`mngr/<name>`),
   then reports `done`.
3. You **record the known-good revision, then merge** the worker's branch on a
   clean `done`.
4. You **reveal** the change with one command, which refreshes dependencies,
   rebuilds/restarts as needed, verifies the live UI is healthy, and
   automatically rolls back if anything breaks.

## 1-2. Delegate to a worker

Follow the `launch-task` skill for the mechanics (task file, `create_worker.py
launch`, background-poll the report, handle `done`/`stuck`), with two
specifics for this flow:

- **Launch the worker with the `--template subskill-worker` template** (not the
  default `worker`). That template installs the bundled
  `update-system-interface-worker` sub-skill into the worker's `.agents/skills/`
  tree so the worker can load it.
- **Keep the task brief short and point it at the sub-skill.** You do not need to
  restate how the worker builds or tests anything -- that all lives in the
  sub-skill. The brief only needs:
  - `## What to do`: the actual UI change the user asked for.
  - `## Context`: any specifics (which panel, desired behavior, constraints).
  - `## Success criteria`: what "done" looks like for this change, plus the
    standing line: *follow the `update-system-interface-worker` sub-skill for
    how to run, test, verify, and what not to touch; report `done` only when its
    testing contract and the review gates all pass.*

## 3. Record known-good, then merge on a clean `done`

Handle the worker's report per `launch-task` (its `## 4` and the referenced
`lead-proxy.md`). On terminal `done`:

1. **Capture the known-good revision first** -- the served branch's current
   `HEAD`, *before* you merge. This is what the reveal rolls back to if the
   change breaks:
   ```bash
   ROLLBACK_TO=$(git rev-parse HEAD)
   ```
2. **Merge** the worker's branch (`mngr/<name>`) into the working branch the live
   UI is served from. Commit the merge so the tree is clean (the reveal refuses
   to run on a dirty tree, so a rollback can never clobber unrelated work).

On `stuck` or a timeout with a dead worker, surface to the user per
`launch-task`'s failure flow -- **do not** reveal anything and do not retry
silently.

Note: the built `static/` bundle is gitignored, so the merge brings only source
and dependency-manifest (`pyproject.toml` / `package.json` / lockfile) changes,
not the worker's build output. The reveal step rebuilds it.

## 4. Reveal the change (after merge)

Run the reveal script with the known-good revision you captured:

```bash
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py --rollback-to "$ROLLBACK_TO"
```

That single command owns the whole reveal as one deterministic, self-healing
motion -- you do not run `npm`/`uv`/`mngr` by hand. It:

- **Classifies** what the merge changed (frontend source, frontend manifest,
  backend source, backend manifest).
- **Refreshes dependencies only if a manifest changed** -- `npm ci` for the
  frontend, `uv tool install -e apps/system_interface --reinstall` for the
  backend. This is essential: a plain restart does *not* re-resolve the
  editable-installed tool's dependencies, so a backend dependency addition would
  otherwise crash the service on restart.
- **Pre-flights a backend change** by booting the merged code on a throwaway port
  before touching the live service. If it can't boot, the live service is never
  restarted -- the UI never goes down.
- **Reveals**: rebuilds the gitignored `static/` bundle and broadcasts a
  `reload_system_interface` op so open browsers reload into the new assets
  (frontend); restarts the services agent so the editable backend re-imports the
  merged `.py` (backend). Restarting does not kill you -- you (a chat agent) and
  the services agent are distinct agents sharing one work_dir.
- **Verifies** the live service is healthy by polling its loopback endpoint.
- **Auto-rolls-back on any failure**: restores the tree to `--rollback-to` as a
  forward revert commit, rebuilds/restarts from it, and re-confirms the UI is
  healthy.

Interpret the exit code and report it to the user:

- `0` -- revealed; the live UI is updated and healthy.
- `2` -- the change was bad and was **automatically rolled back**; the live UI is
  healthy on the previous revision, but the requested change did **not** land.
  Report this and diagnose before retrying.
- `3` -- **emergency**: even rollback could not restore a healthy UI. The
  interface may be down; escalate immediately.
- `1` -- precondition error (e.g. a dirty tree); nothing was changed.

Why this exists as a script and not a checklist: if the backend fails to start,
the user loses their entire chat UI -- there is nowhere left to surface an error
message. The recover-or-revert logic must therefore run identically every time
and can never be skipped, which is exactly what belongs in a deterministic script
rather than agent prose.

`scripts/layout.py refresh` (the `manage-layout` skill) is unrelated -- it only
reloads a single inner iframe/panel for arranging the workspace, not the
top-level page, so it does **not** reveal a system-interface code change.

## Why this shape

The UI is what the user is actively looking at, so the design goal is "never
serve a half-broken UI," not "iterate in place fast." The worker's isolated
worktree clone + in-process testing + Playwright verification + review gates make
it safe to merge; the reveal script's pre-flight, health probe, and autonomous
rollback make it safe to reveal in one motion.
