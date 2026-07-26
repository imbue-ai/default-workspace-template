# Your workspace

This folder is your mind's home: everything it knows, everything it builds,
and the machinery that keeps it running.

## What's here

- `creations/` - Things your mind has built for you: apps, dashboards, tools,
  and services. Each creation lives in its own folder.
- `data/` - Your workspace's data: files you've uploaded, files shared back to
  you in chat, your mind's memories, and each creation's stored data.
- `docs/` - Guides to this workspace: what it is, how it works, and a history
  of where it came from.
- `system/` - The machinery that runs the workspace: background services,
  scripts, and configuration. You can look around (every folder has a README),
  and your mind maintains it for you.

A few housekeeping files live alongside them:

- `README.md` - This file.
- `CLAUDE.md` - The standing instructions your mind follows.
- `pyproject.toml` and `uv.lock` - The Python project definition; the tooling
  requires them at the top level.

## Where things are kept safe

The workspace is a git repository: code and configuration changes are
committed as your mind works. Everything under `data/` is deliberately kept
out of git (it can be large, personal, or both) and is protected by the
workspace's continuous encrypted backup instead, along with the rest of the
workspace. See `docs/` for details.
