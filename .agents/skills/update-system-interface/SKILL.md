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
3. You **merge** the worker's branch on a clean `done`.
4. You **reveal** the change. A frontend change is rebuilt + reloaded in place;
   a backend change is a service restart.

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

## 3. Merge on a clean `done`

Handle the worker's report per `launch-task` (its `## 4` and the referenced
`lead-proxy.md`). On terminal `done`, merge the worker's branch (`mngr/<name>`)
into the working branch the live UI is served from. On `stuck` or a timeout with
a dead worker, surface to the user per `launch-task`'s failure flow -- **do not**
reveal anything and do not retry silently.

Note: the built `static/` bundle is gitignored, so the merge brings only
`frontend/src/` changes, not the worker's build output. The reveal step rebuilds
it.

## 4. Reveal the change (after merge)

Reveal depends on what changed. The running server serves the frontend bundle
from `static/` on disk, so a frontend change needs no restart -- just a rebuild
and a browser reload. A backend (`.py`) change needs the server process to
restart.

**Frontend change** (anything under `frontend/src`): rebuild the gitignored
bundle, then tell open browsers to reload into it:

```bash
( cd apps/system_interface/frontend && npm run build )
python3 .agents/skills/update-system-interface/scripts/reload_system_interface.py
```

The reload script broadcasts a `reload_system_interface` op over the websocket;
the dockview shell responds by reloading the whole top-level page (picking up the
new hashed assets and every child chat iframe). With no browser connected it's a
harmless no-op.

**Backend change** (anything under `imbue/`): restart the services agent so the
editable-installed `system-interface` backend re-imports the merged `.py`:

```bash
mngr start --restart system-services
```

This does not kill you: you (a chat agent) and the services agent are distinct
agents sharing one work_dir. (If a change touches both, do both: rebuild + reload
for the frontend, restart for the backend.)

`scripts/layout.py refresh` (the `manage-layout` skill) is unrelated -- it only
reloads a single inner iframe/panel for arranging the workspace, not the
top-level page, so it does **not** reveal a system-interface code change. Use the
`reload_system_interface.py` script above for that.

## Why this shape

The UI is what the user is actively looking at, so the design goal is "never
serve a half-broken UI," not "iterate in place fast." The worker's isolated
worktree clone + in-process testing + Playwright verification + review gates are
what make it safe for you to merge and reveal in one motion.
