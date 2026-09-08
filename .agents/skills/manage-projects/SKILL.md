---
name: manage-projects
description: Use when the user's request is about projects/views themselves -- which project an object belongs to, what a project shows, adding something to a project, the sharp edge where a renamed object can't be addressed by its new name, or who can rename what. Not for ordinary tab arrangement (split/move/focus within a view) -- see manage-layout for that.
---

# Managing workspace projects

The workspace shows one *view* at a time: a **project**, or **Everything**
(the unfiltered home). This skill covers the project/view layer itself --
what a project is, how to query membership, how membership actually
changes, and two sharp edges (ref resolution, renaming) that are easy to
get wrong. For the mechanics of `system/scripts/layout.py` itself --
panel refs, `split`/`move` directions, exit codes, wait-stable diffs --
see the `manage-layout` skill; this one assumes you've read it.

## What a project is

A project is a **view**: a filter over the machine's objects (chat
agents, terminal sessions, browsers, registered apps) plus its own
saved dockview arrangement. It is *not* a container -- nothing owns
anything:

- **Membership is many-to-many.** The same object can sit in any
  number of projects at once. There is no "move" -- filing it
  somewhere new never takes it out of anywhere else.
- **A member with no open tab is *backgrounded*** -- still running,
  still listed in that project's rail, just not docked. Closing a tab
  never changes membership; only filing (below) and the UI's "remove
  from project" (a verb on the object, offered by its rail row -- the
  row's own one-click control, or its kebab/right-click menu) do.
- **Only the destructive per-kind verbs actually end something**
  (Delete, offered from the tab/rail menu for chats, terminals, browser
  sessions, and app instances -- not exposed through `layout.py`), and
  those take the object out of *every* project at once, including ones
  with no client currently looking at them. Deleting an app INSTANCE
  removes only that instance's references (memberships, panes, name,
  recency, stored location); the app's service keeps running. The
  service itself gets the reversible Stop/Start instead (`POST
  /api/apps/<name>/stop` / `/start`, supervisord-backed, only for apps
  registered with a `program`), which changes no membership and no
  registry row -- a stopped app's instances stay listed, dimmed.
  Actually removing an app is the mind's job via the `update-app` skill;
  the `POST /api/apps/<name>/deregister` endpoint remains for tooling
  but no UI surface calls it.

**Everything** is the unfiltered view and the home. It is not a
project: it has no registry entry, no member list, and cannot be
renamed or deleted -- but it keeps its own saved arrangement like any
other view, and its tab list enumerates the whole machine, so an
object in no project at all still shows up there.

Each project keeps its metadata (name, color, glyph, member list) in
one machine-wide registry, plus one saved arrangement file per device
kind (desktop / mobile) -- membership is shared across devices, only
tab placement differs.

## Member ref grammar

A project's member list is a list of refs, in this grammar (verified
against `system/apps/system_interface/imbue/system_interface/projects.py`'s
`_panel_member_ref`):

| Form | Meaning |
|---|---|
| `chat:<agent-id>` | A chat, filed under the agent's stable id (not its renameable display name, so membership survives a rename). |
| `terminal:<tmux-session-name>` | A terminal, filed under its live tmux session name -- the identity *is* the name here. |
| `service:<name>?instance=<name>-<N>` | One numbered INSTANCE of a registered app (`service:files?instance=files-2`, shown as "File Viewer 2"). Instances are what the tab lists hold; each is a separate object with its own page, filed and deleted independently. |
| `service:<name>` | A registered app's PIN in a project (which app the project keeps a rail shortcut for). Not a tab-list object: opening it resolves to the view's most recently used instance, minting `<name>-1` when it shows none. |
| `service:browser?session=<id>` | One pane of the browser fleet; the `session` query makes each pane a distinct member. |

An instance exists while anything references it -- a project's member
list, or a pane in any view's saved layout -- and deleting it (the UI's
confirm-gated Delete, which also fires when its last reference goes)
just removes those references; the app's service keeps running. The
backend mints instance names machine-wide, lowest free number first
(`POST /api/apps/<name>/instances/allocate`), and
`GET /api/apps/instances` lists every instance the machine holds.

`url:<hash>` is a **legacy form only** on this branch: an ad-hoc
external-URL tab has no identity beyond the panel showing it (its
"member ref" would be a hash of the panel id, which can't outlive the
panel), so opening a URL tab no longer files it as a member at all. A
`url:<hash>` entry you see in a project's member list is a leftover
from a machine that predated projects (the one-time migration filed
panels it couldn't otherwise name this way). Such a leftover is removed
from its rail row like any other member -- the row's one-click remove is
the only verb it has, since a `url:` ref carries no menu.

This is a *different* grammar from the live-panel refs `layout.py`
resolves to (see `manage-layout`): `chat-terminal:<name>`,
`subagent:<id>`, and the opaque `terminal:<hash>` / `url:<hash>` an
anonymous terminal or ad-hoc URL panel gets *as a panel* all address a
specific dockview tab instance, not membership.

## Query: what views exist, and what's in them

```bash
# Every view (every project, plus Everything): id, name, member refs,
# whether it has desktop/mobile content saved yet, and which connected
# clients currently have it in front.
python3 system/scripts/layout.py views

# Everything addressable (registered services and chat agents), with
# open/running flags scoped to one view.
python3 system/scripts/layout.py list --view "Research"

# What's actually docked in a view right now (only *open* tabs -- a
# backgrounded member with no panel will not appear here).
python3 system/scripts/layout.py inspect --view "Research"

# Which client asked, on which view -- use this to resolve an
# unqualified request to the right project.
python3 system/scripts/layout.py context
```

`views`' `members` field is the full membership (open and
backgrounded); `inspect`'s panel list is only what currently has a
tab. Diff the two to see which members of a project are backgrounded:
a ref in `views`' `members` for that project id but absent from
`inspect --view <name>`'s panels is backgrounded there. There is no
single `layout.py` call that answers "which projects is ref X in" --
run `views` and scan every project's `members` list for the ref (the
REST surface has a dedicated `GET /api/projects/members` for this, but
`layout.py` does not wrap it).

`list`'s `is_open` flag also reflects the named `--view`, not
membership -- an object can be a member (via `views`) while `list
--view <name>` reports it `is_open: false` because it's backgrounded
there.

## Modify: how membership actually changes

`layout.py` has no `add-member` / `remove-member` subcommand. Filing
happens as a **side effect of opening a tab**: `open` and `split`
adding a panel to a view is the only thing that files an object as a
member of it (idempotent -- opening an already-open, already-filed
ref is a no-op). `close` never touches membership, by design (a
closed tab stays backgrounded, not removed). `focus` / `maximize` /
`restore` / `replace-url` / `refresh` create no new panel, so none of
them change membership either.

```bash
# Opens an instance of "web" in the Research project and files it: the
# view's most recently used instance when it shows one, else a freshly
# minted web-1. No-op if an instance is already open there.
python3 system/scripts/layout.py open web --view "Research"

# Always mint a fresh numbered instance (web-2, web-3, ...); the minted
# service:web?instance=... ref is printed to stdout for later ops.
python3 system/scripts/layout.py open web --new --view "Research"

# Address one specific instance -- open/focus/close/move/refresh all
# accept instance refs.
python3 system/scripts/layout.py focus "service:web?instance=web-2"
python3 system/scripts/layout.py close "service:web?instance=web-2"

# Same, with explicit positioning.
python3 system/scripts/layout.py split web --relative-to=self --direction=right --view "Research"
```

There is no `layout.py` verb for the opposite (removing a ref from a
project without also closing it, or removing it while leaving it
open elsewhere) -- that is the UI's "remove from project", which lives on
the object's own rail row (a one-click control, and the same verb in the
row's menu), or the REST `POST /api/projects/<id>/members/remove`
endpoint the README documents, neither of which `layout.py` wraps.
Likewise, creating, deleting, or renaming a *project itself* (as
opposed to renaming an object shown in one) has no `layout.py`
subcommand -- see "Deleting a project" below.

The rest of the mutating surface -- `focus` / `move` / `maximize` /
`restore` / `replace-url` -- works exactly as `manage-layout`
describes, and accepts the same `--view <name>` to target a project
other than the one the connected client is looking at. `refresh` is
the one exception: it takes no `--view` (it reloads the named iframe
on every connected client, view notwithstanding).

## Rail shortcuts: pins and modes

Each project's rail carries **shortcuts**: the four built-in rows (chat,
files, browser, terminal) plus one row per pinned app. Two facts about
each are per-project state:

- **Pinned** (built-ins only): whether the row sits in the rail or in
  the All apps menu. An app's pin IS its membership, so `app:` shortcuts
  have no separate pin state.
- **Mode**: what clicking the row does. `focus` goes to the most
  recently used member of that kind in the view (creating only when the
  view shows none); `new` always creates. Defaults are code-side: chat
  defaults to `new` ("New Chat"), everything else to `focus`.

Agents can read and change both -- the same endpoint the UI uses, so
one validator covers both writers:

```bash
# The active (or named) view's effective shortcut list: id, pinned, mode.
python3 system/scripts/layout.py shortcuts --view "Research"

# Unpin the terminal row into Research's All apps menu.
python3 system/scripts/layout.py shortcut set terminal --unpin --view "Research"

# Make the docs app's shortcut always open a fresh pane.
python3 system/scripts/layout.py shortcut set app:docs --mode new --view "Research"

# Put the chat shortcut in focus mode (its default is new).
python3 system/scripts/layout.py shortcut set chat --mode focus --view "Research"
```

`shortcut set` refuses Everything (it has no project entry to store
state against; its shortcuts are always the defaults) and refuses
`--pin/--unpin` on an `app:` key (pin an app by opening it in the view,
unpin with the UI's remove-from-project or the REST member-remove
endpoint). Storage is a sparse `shortcut_overrides` map on the
project's registry entry -- only deviations from the defaults persist.

## The trap: ref resolution only ever sees the registered name

Every `<ref-or-service>` argument to `layout.py` accepts a bare word
as shorthand for `service:<word>` (`_normalize_ref`, verified in
`system/scripts/layout.py`) -- it does a literal string
substitution, nothing more:

```bash
python3 system/scripts/layout.py open Docs
# -> normalizes to service:Docs, then waits for a service literally
#    named "Docs" to appear in data/.state/apps.toml
```

If the app's *registered* service name is `web` but someone renamed
its tab to "Docs" (`layout.py rename service:web "Docs"`, or the
title otherwise ended up set), that rename never touches the
registered name -- it only sets a display title. `open Docs` above
still expands to `service:Docs`, which is not registered, and fails
after the registration wait with exactly:

```
error: service 'Docs' is not registered in data/.state/apps.toml after waiting 5s. Did you forward_port.py / start the service?
```

**The fix:** don't guess the ref from the name the user said. Run
`inspect --json` (or `--verbose`) for the view in question and find
the panel whose `title` is `"Docs"` -- its `ref` field
(`service:web`) is what you actually pass to `open` / `focus` / etc.
This only works for a panel that's currently *open*: `inspect` only
lists docked panels, and `layout.py` has no subcommand that reads the
machine-wide title store (`workspace_layout/member_titles.json`)
directly, so a *backgrounded* renamed object's chosen title isn't
recoverable through `layout.py` at all -- you'd need the REST
`GET /api/member-titles`, or to ask the user which object they mean.

Services, terminals, and browsers are where this bites hardest,
because their registered/derived identity can never change -- the
chosen title and the addressable name diverge permanently the moment
someone renames the tab. A `chat:` ref is different in kind: renaming
a chat goes through `mngr rename`, which changes the agent's actual
registered name rather than layering a display-only title on top (see
below), so its ref is not frozen the same way -- but that also means
a ref you captured before the rename (`chat:alice`) will not
necessarily still resolve afterward. Either way, re-resolve the ref
from a fresh `inspect` rather than assuming a ref captured earlier is
still current.

## Renaming: chat-only from the UI, any kind from `layout.py`

The tab-rename gesture (double-click a tab title, or its menu's
Rename) is offered **only for a chat** in the UI. That's not an
arbitrary restriction:

- A **chat** is an mngr agent. Its ref is a stable agent id, and
  `mngr rename` moves the display name everywhere the agent is known
  (its `display_name` label plus the canonical true name derived from
  it), so the name you give it is a name anything else -- an agent
  included -- can address it by. `mngr list` and the tab always agree.
- A **terminal** *is* its live tmux session name, and a **browser**
  *is* its Chromium profile directory -- there is no separate display
  name to change, so both keep their derived numbering ("Terminal 3",
  "Browser 1") and get no rename gesture at all.
- An **app** has a stable id (its registered service name), but that
  name is also the *only* handle `layout.py` accepts -- as the trap
  above shows. A chosen title you can read but can't then address by
  is worse than no title, so the UI withholds the gesture here too.

`layout.py rename <ref-or-service> <title>`, by contrast, accepts
**any** ref kind -- it is the one path that can put a display title on
a terminal, browser, or app, even though the UI itself never offers
that gesture:

```bash
python3 system/scripts/layout.py rename chat:alice "Alice (lead)"
python3 system/scripts/layout.py rename web "Docs"
```

For a `chat:` ref this really does call `mngr rename` (the agent's
actual name changes). For every other kind it writes the display
title to the machine-wide `member_titles.json` store only -- the
registered/derived identity underneath is untouched, which is exactly
what makes the trap above possible. `rename` also requires the ref to
already be **open** in the target view (`_require_open`) -- it cannot
rename a backgrounded member with no live panel; open it first.

## Deleting a project is a pure view operation

Right-click a project in the rail -> project settings -> delete (or
the REST `POST /api/projects/<id>/delete`) removes the project's
registry entry and its saved arrangement files. Nothing else happens:

- Every object the project showed keeps running.
- Every object stays in every other project that shows it, and in
  Everything (which has no member list to remove anything from).
- A machine can end up with zero projects; clients sitting in the
  deleted one fall back to another project, or to Everything if none
  remain.

`layout.py` has no subcommand for this (there is no `create` / `delete`
/ rename-the-project-itself verb in its parser) -- it's a UI-only
gesture, or a direct REST call, never something to reach for through
the agent-facing script.
