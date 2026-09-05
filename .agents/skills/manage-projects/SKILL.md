---
name: manage-projects
description: Use when the user's request is about projects/views themselves -- what a project shows, adding or removing a tab from a project, the project's rail shortcuts, or what happens to an instance when it is deleted. Not for ordinary tab arrangement (split/move/focus within a view) -- see manage-layout for that.
---

# Managing workspace projects

The workspace shows one *view* at a time: a **project**, or **Everything**
(every instance on the machine). This skill covers the project/view layer
itself: what a project is, how to query it, how its tab set and rail
shortcuts change, and what deleting an instance or a project does. For the
mechanics of `system/scripts/layout.py` (addresses, `split`/`move`
directions, exit codes, wait-stable diffs) see the `manage-layout` skill;
this one assumes you've read it.

## What a project is

A project is a **view**: a shared **tab set** (the addresses it shows) plus
its own saved arrangement per client. It is *not* a container: nothing owns
anything.

- **A tab set is many-to-many.** The same instance can sit in any number of
  projects at once. Adding it somewhere new never takes it out of anywhere
  else.
- **An instance in the tab set with no open panel is *backgrounded***: still
  running, still in the project's rail, just not docked in that client's
  arrangement. Closing a tab never changes the tab set; only the tab-set
  routes (below), the rail row's "remove from project", and the instance
  going away do.
- **Deleting an instance ends it everywhere** (`layout.py delete <address>`,
  or the tab/rail menu's Delete): the app that owns it deletes it, the shell
  sees it leave the app's list, and every project's tab set and every
  client's layout drop it. The app itself keeps running. An app with a
  supervised program gets the reversible Stop/Start instead (`POST
  /api/apps/<name>/stop` / `/start`), which changes no tab set; a stopped
  app's instances stay listed, dimmed. Critical apps (the shell, the chat)
  cannot be stopped from the workspace.
- **Referenced instances** (a file viewer, a chat's subagent view) live only
  while some tab set or client layout references them. Once the last
  reference goes, the shell asks the app to delete the instance, after a
  short grace period for a tab that was just created. Explicit instances (a
  chat, a terminal, a browser) stay until deleted.

**Everything** is the unfiltered view and the home. It is not a project: it
has no tab set of its own (its tabs are every instance on the machine), and
it cannot be renamed or deleted; it keeps a saved arrangement per client like
any other view.

The shell keeps every project's metadata (name, color, glyph, tab set,
shortcuts) in `data/.state/system_interface/projects.json`, and one
arrangement per view per client under `data/.state/system_interface/layouts/`,
plus a seed per device kind that a client without an arrangement of its own
starts from.

## Query: what views exist, and what's in them

```bash
# Every view (every project, plus Everything): id, name, tab set, and which
# connected clients currently have it in front.
python3 system/scripts/layout.py views

# Every app and instance on the machine, each with its address, title,
# status, and the clients whose layouts dock it (scoped to one view's layouts).
python3 system/scripts/layout.py list --view "Research"

# What is actually docked in a view right now (only *open* panels; a
# backgrounded tab-set entry with no panel will not appear here).
python3 system/scripts/layout.py inspect --view "Research"

# Which client asked, on which view.
python3 system/scripts/layout.py context
```

`views`' `tabs` field is the full tab set (open and backgrounded); `inspect`'s
panel list is only what currently has a panel in that client's arrangement.
Diff the two to see which entries are backgrounded. To answer "which projects
show address X", run `views` and scan every project's `tabs`.

## Modify: how a tab set changes

Opening a tab in a project files it: `open` and `split` add the address to
the project's tab set (idempotent). `close` never touches the tab set (a
closed tab is backgrounded, not removed). `focus` / `maximize` / `restore` /
`refresh` create no panel, so they change nothing either.

```bash
# Dock an existing file viewer in the Research project (and file it in the tab set).
python3 system/scripts/layout.py open "app:files?instance=files-1" --view "Research"

# Create a fresh terminal in Research; its address is printed to stdout.
# (`open files` creates a fresh file viewer the same way.)
python3 system/scripts/layout.py open terminal --view "Research"

# Address one specific instance anywhere.
python3 system/scripts/layout.py focus "app:terminal?instance=terminal-2"
python3 system/scripts/layout.py close "app:terminal?instance=terminal-2"
```

`layout.py` has no verb for adding an address to a project *without* opening
it, or for removing one while leaving it open elsewhere. Those are the rail
row's "remove from project" in the UI, or the shell's REST routes, which the
`system_interface` README documents:

```bash
curl -s -X POST http://127.0.0.1:8000/api/projects/research/tabs \
    -H 'Content-Type: application/json' -d '{"address": "app:files?instance=files-1"}'
curl -s -X POST http://127.0.0.1:8000/api/projects/research/tabs/remove \
    -H 'Content-Type: application/json' -d '{"address": "app:files?instance=files-1"}'
```

Creating, deleting, or renaming a *project itself* has no `layout.py`
subcommand either: it is the rail's project settings dialog, or the REST
routes (`POST /api/projects`, `POST /api/projects/<id>/settings`, `POST
/api/projects/<id>/delete`).

## Rail shortcuts

Each project's rail carries **shortcuts**: one row per (app, action), in
rail order. A new project is seeded with every app's `default_shortcut` from
its manifest (the chat's "New Chat" in new mode; the terminal's "New
Terminal", the files app's "New File Viewer", and the browser's "New Browser"
in focus mode). Each row has a **mode**: `focus` goes to the app's most recent
tab in the view (running the action only when it has none); `new` always runs
the action. Everything's rail is fixed: every app's primary action, in registry order
(its `default_shortcut` action, else its first declared action; a
single-instance app's synthesized `open`; an app with instances that declares
no action has no row).

```bash
# A view's rail: app, action, mode.
python3 system/scripts/layout.py shortcuts --view "Research"

# Add the docs app's "open" to Research's rail, always creating anew.
python3 system/scripts/layout.py shortcut set docs open --mode new --view "Research"

# Put the chat shortcut in focus mode (its default is new).
python3 system/scripts/layout.py shortcut set chat new --mode focus --view "Research"

# Take a row off the rail (it stays available from the "All apps" popover).
python3 system/scripts/layout.py shortcut remove docs open --view "Research"
```

`shortcut set` refuses an action the app does not declare, and refuses
Everything.

## Titles: whose they are

An instance's title belongs to the app that owns it, and `layout.py rename
<address> "<title>"` asks that app to change it. A chat rename goes through
`mngr rename`, so the agent's actual name changes; a terminal's title is its
own (its tmux session name stays its key); a browser is not renameable, so
`rename` on one is refused. Renaming never
changes an address: the key is stable for the instance's life, so a title you
gave something is not a handle you can address it by. When the user names a
tab by its title, find its address with `layout.py list` (the row whose
`title` matches) rather than guessing.

## Deleting a project is a pure view operation

The rail's project settings -> delete (or `POST /api/projects/<id>/delete`)
removes the project and its saved arrangements. Nothing else happens: every
instance the project showed keeps running, stays in every other project that
shows it, and stays in Everything. Referenced instances that the deleted
project alone referenced are deleted by their apps, as for any lost
reference. A machine can end up with zero projects; clients sitting in the
deleted one fall back to another project, or to Everything if none remain.
