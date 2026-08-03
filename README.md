# Your workspace

This folder is your mind's home: everything it knows, everything it builds,
and the machinery that keeps it running.

## Creations

Broadly, in Minds you make "creations". These can be "code" (apps, skills, and
the services behind them) or "data" (documents, images, notes).

Minds makes this easier by defining some conventions for the common things
you'll want to make:

1. an "app" - something you can open as a tab and interact with
2. a "skill" - teaches your mind how to do work you care about. A skill that
   is automatically run on a schedule is called an "automation" (the
   machinery that runs them lives in `system/libs/automations/`; the weekly
   Caretaker is the built-in example)
3. some "data" - documents, images, notes, or data created by your apps and
   skills
4. some "customizations" - changes to any of the above. Everything in Minds
   can be modified by you!

## What's here

- `apps/` - Everything you can open as a tab: the built-in apps (the
  terminal, the browser) and the apps your mind builds for you. (A shortcut
  to `system/apps/`.)
- `skills/` - Everything your mind knows how to do: the built-in skills and
  the ones it has learned for you. (A shortcut to `.agents/skills/`.)
- `data/` - Your workspace's data: documents and project folders, files
  you've uploaded, your mind's memories, and each app's stored data.
- `docs/` - Guides to this workspace: what it is, how it works, and a history
  of where it came from.
- `system/` - The machinery that runs the workspace: the apps themselves,
  background services, scripts, and configuration. You can look around (every
  folder has a README), and your mind maintains it for you.

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
