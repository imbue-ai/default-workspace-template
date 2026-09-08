---
name: manage-layout
description: Use when you want to rearrange the workspace dock tabs (open, split, move, focus, close, maximize, reload, rename, delete, stop or start an instance) or inspect the live layout.
metadata:
  author: imbue
  crystallized: true
---

# Managing the workspace dock layout

The user interacts with you (and the apps you build) through a tabbed dock
defined in `system/apps/system_interface`. Your chat is one such tab; every
other tab is an instance of some app: a terminal, a browser, a files page,
another agent's chat, an app you built.

`system/scripts/layout.py` is the agent-facing helper. Use it whenever you
want to surface, inspect, or rearrange tabs. Do not hand-edit the dock's
saved layouts.

> **Where the script lives:** `layout.py` is at the **repo root**, at
> `system/scripts/layout.py` (i.e. `/home/user/workspace/system/scripts/layout.py`,
> the container WORKDIR). It is **NOT** inside this skill's folder. Every
> command below is written as `python3 system/scripts/layout.py ...`, a path
> relative to the repo root, which is the cwd for all commands in this repo.

## Addresses (read this first)

Every tab is named by one **address**:

| Form | Meaning | Example |
|---|---|---|
| `app:<name>?instance=<key>` | One instance of an app. | `app:chat?instance=agent-3f2a...` (a chat, keyed by its agent id), `app:terminal?instance=terminal-2` (a terminal, keyed by its tmux session name), `app:browser?instance=riley` (a browser, keyed by its name) |
| `app:<name>` | A single-instance app's one tab (an app built without `instances = true`); or, as an `open` / `split` target for an app with instances, "a fresh instance of this app". | `app:docs` (a single-instance app you built), `open app:terminal` |

A bare word is shorthand for `app:<word>` (`open files`). The literal `self`
resolves to your own chat panel; most useful as `--relative-to=self` on
`split` / `move`. Your own chat's address is `app:chat?instance=$MNGR_AGENT_ID`.

`layout.py list` prints every address on the machine, with each instance's
title and status, so you never have to guess: find the row whose title the
user said, and use its address.

A bare `https://` URL is also an `open` target: `open https://example.com`
starts a new browser on that page (the browser app's `new` action with the URL
as its `url` param) and prints the new browser's address.

## Clients and views

The workspace shows one *view* at a time: a **project** (a shared set of tabs
plus its own arrangement) or **Everything** (every instance on the machine).
Every browser **client** (one per browser; its windows share it) has one
active view and its own arrangement of every view, kept in a file on the
shell. That file is the truth: the browser saves the user's own gestures into
it, and the shell edits it for your ops, so an op lands whether or not a
browser is connected and a connected window shows it within a redraw.

- **Every op targets exactly one client.** With no `--client`, that is the
  client that most recently messaged you, else the one connected client.
  When neither settles it (several clients, an agent nobody messaged), the op
  is refused with the connected clients listed; pass `--client <id>` (from
  `context`). Ops are never applied to every client at once.
- **An op with no `--view` edits the client's active view.** That is what
  you want nearly always; just run the op.
- **Pass `--view <name>` to edit a different view** (a project's name, or
  `Everything`). The op edits that view's arrangement and switches the client
  to it, so the user sees what you arranged.
- **`views` lists the views**: every project plus Everything, each with its
  tab set and which connected clients have it in front.
- **`context` tells you which client asked**: every known client with its
  device kind, active view, connection state, and last few messages. The
  client that most recently messaged you is almost always the requester.
- **`load <view>` switches a client onto a view** without changing any
  arrangement (`load "Research"`).

Every tab you open in a project is filed into that project's tab set, so it
shows in the project's rail and on every device.

## The verbs you'll use 95% of the time

| Goal | Command |
|---|---|
| See which client/view asked for something | `python3 system/scripts/layout.py context` |
| List every app and instance (address, title, status, where docked) | `python3 system/scripts/layout.py list` |
| List the views and who is on each | `python3 system/scripts/layout.py views` |
| See what's currently open and how it's laid out | `python3 system/scripts/layout.py inspect [--view <name>]` |
| Locate one panel, its tab-mates, and its neighbors | `python3 system/scripts/layout.py where <address> [--view <name>]` |
| Switch a client onto a view | `python3 system/scripts/layout.py load <view> [--client <id>]` |
| Surface an instance alongside your chat | `python3 system/scripts/layout.py open <address>` |
| Open a web page in a new browser | `python3 system/scripts/layout.py open https://example.com` |
| Create an instance with arguments | `python3 system/scripts/layout.py open terminal --param workdir=/data` |
| Put a new terminal in the same tab group as your chat | `python3 system/scripts/layout.py split terminal --relative-to=self --direction=within` |
| Close a tab | `python3 system/scripts/layout.py close <address>` |

`open` is the opinionated default. It puts the new tab to the right of your
chat, joining whatever group already lives there if one is open. Pass
`--new-group` to force a fresh column instead.

What `open` does with each target:

- `open app:docs` (a single-instance app, one you built without
  `instances = true`): docks its one tab, or brings it to the front if it is
  already open.
- `open app:terminal?instance=terminal-2` (an instance address): docks that
  instance, or brings it to the front if it is already open.
- `open terminal` (a bare app that has instances): runs the app's action
  through the app and creates a **fresh** instance every time, exactly like
  the rail's "New Terminal". The new instance's address is printed to
  **stdout** so you can capture it for later ops. The same holds for `open
  chat` (a new chat), `open browser` (a new browser), and `open files` (a new
  file viewer: the files app has instances too, so `app:files` never names an
  open viewer). `--action <id>` picks another of the app's actions and
  `--param name=value` (repeatable) passes the create's params: `open
  terminal --param workdir=/data`, `open files --param path=/data/notes`. An
  app's refusal (a full browser fleet, no signed-in account) is the op's
  error, printed as the app spelled it.
- `open https://example.com` (a URL): a new browser on that page.

## Less common operations

All of these take the same `--client` as `open`; `split`, `focus`, `move`
take `--view` too. `maximize`, `restore`, and `refresh` change what is on the
target client's screen without changing the saved arrangement (a `refresh` of
a whole app reloads its iframes on every client):

| Goal | Command |
|---|---|
| Place a new panel with explicit positioning | `python3 system/scripts/layout.py split <address> --relative-to=<address> --direction=<left\|right\|above\|below\|within> [--ratio=0.4] [--new-group]` |
| Focus an existing tab | `python3 system/scripts/layout.py focus <address>` |
| Move an open tab next to / into another's group | `python3 system/scripts/layout.py move <address> --relative-to=<address> --direction=<dir> [--new-group]` |
| Maximize / restore a group | `python3 system/scripts/layout.py maximize <address>` / `python3 system/scripts/layout.py restore` |
| Reload one tab (or every iframe of an app) | `python3 system/scripts/layout.py refresh <address>` |

And five verbs that go through the app that owns the instance rather than
the dock (they take an instance address, never a bare app):

| Goal | Command |
|---|---|
| Retitle an instance (the title shows in every view) | `python3 system/scripts/layout.py rename <address> "<title>"` |
| Delete an instance (it leaves every view) | `python3 system/scripts/layout.py delete <address>` |
| Point an instance at a path under its app, or at a URL for an app that browses to one | `python3 system/scripts/layout.py replace-url <address> </path-or-url>` |
| Stop what backs an instance while keeping it (a chat's agent, a browser's Chromium, a terminal's session) | `python3 system/scripts/layout.py stop <address>` |
| Start a stopped instance again | `python3 system/scripts/layout.py start <address>` |

Not every app accepts every verb: a browser is not renameable (its title is
its name), an app that does not track locations (the terminal, the chat)
refuses `replace-url`, each app takes only the location form that fits it
(a path under the app for the file viewer, an absolute `http(s)` URL for the
browser), and only an instance the app lists as `stoppable` accepts `stop`
and `start` (a file viewer has nothing to stop). The app's refusal is
printed as the error.

### Directions on `split` and `move`

`--direction` takes five values:

- `left` / `right` / `above` / `below` describe the **adjacent group** in
  that direction relative to the anchor. By default the panel tabs into a
  group that already lives there; pass `--new-group` to carve a fresh
  column / row instead so both panels are visible at once.
- `within` describes the **anchor's own group**: the panel becomes a tab
  inside it. `--new-group` is meaningless with `within` and is rejected.

The most common natural request, "put a new terminal in the same tab group
as my chat", is:

```bash
python3 system/scripts/layout.py split terminal --relative-to=self --direction=within
```

## Inspecting state

`inspect` defaults to a compact, one-line-per-group rendering:

```
active_panel: g1
row size=1.0
  [app:chat?instance=agent-3f2a* app:terminal?instance=terminal-1] size=0.4
  [app:files?instance=files-1*] size=0.6
```

The `*` marks the active tab in each group. `row` means the children sit
side by side, `column` means they stack. Pass `--verbose` for the full YAML
tree (with each panel's tab id and title) or `--json` for the structured
object. `where <address>` zeros in on one panel: its title, its group's tabs,
and the tabs in each cardinal direction.

`list` prints every app with its instances: each instance's address, title,
status (`idle`, `working`, `attention`, `stopped`, `error`), and the ids of
the clients whose layouts dock it. `list` and `views` output YAML by default;
pass `--json` for programmatic consumption.

Run `python3 system/scripts/layout.py --help` (or `<subcommand> --help`) for
the full surface.

## Ops answer at once

The shell edits the client's arrangement itself and answers with the result,
so every dock op returns as soon as the file is written. `open`, `split`,
`move`, `focus`, and `close` print a one-line description on **stderr**
(`opened app:terminal?instance=terminal-2 in tabs=[...]`, `moved ... in
...`); opening an address that is already open focuses it. `maximize`,
`restore`, and `refresh` print `(sent <op> to client <id>)`.

**stdout** is reserved for machine-readable output: the address of an
instance `open` / `split` created, and the structured output of the read
commands. Descriptions always go to stderr.

## Exit codes

- `0` ok (including no-op successes)
- `1` error (the specific reason is in stderr, including "could not tell
  which client this op is for", for which you pass `--client <id>` from
  `context`, and an address that is not open or that no app lists)
- `3` the app cannot do it right now (a full browser fleet, no signed-in
  account for a new chat, an app still starting up; its 409 or 503): retry
  after a short backoff, or tell the user

## When NOT to use this skill

- **Building a brand-new app.** Use `build-app` to scaffold it first; it
  ends with a `layout.py open` call to surface the new tab.
- **Projects themselves** (what a project shows, its rail shortcuts, adding
  a tab to a project without opening it): see `manage-projects`.
- **Persisting layout state.** The frontend auto-saves on every change.
