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
4. You **reveal** the change with a single restart.

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

Run a single command:

```bash
mngr start --restart system-services
```

This cleanly restarts the services agent (its tmux session -> bootstrap -> all
services). On startup the system_interface service rebuilds the frontend bundle
**if** `frontend/src` (or the lockfile) changed since the last build, and the
editable-installed `system-interface` backend picks up the merged `.py` source.
Any browser the user has open reloads itself into the new bundle once its
connection re-establishes (an in-page build-id check, see the
`update-system-interface-worker` sub-skill and `apps/system_interface/README.md`),
so there is no separate build-or-reload step to run.

This does not kill you: you (a chat agent) and the services agent are distinct
agents sharing one work_dir.

`scripts/layout.py refresh` (the `manage-layout` skill) is unrelated -- it only
reloads inner iframes/panels for arranging the workspace, not for revealing a
system-interface code change.

## Why this shape

The UI is what the user is actively looking at, so the design goal is "never
serve a half-broken UI," not "iterate in place fast." The worker's isolated
worktree clone + in-process testing + Playwright verification + review gates are
what make it safe for you to merge and reveal in one motion.
