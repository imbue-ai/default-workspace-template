---
name: manage-desktop
description: Use when you want to open, arrange, or close windows on the user's desktop (open an app or a page, focus, place, minimize, maximize, close, navigate, refresh), switch or read desktops, edit a desktop's shortcuts or wallpaper, or work out which screen a request came from.
metadata:
  author: imbue
  crystallized: true
---

# Managing the workspace desktop

The user interacts with you (and the apps you build) through a desktop defined
in `system/apps/system_interface`: windows over a backdrop, a taskbar, and a
launcher. Your chat is one window; every other window is a page of some app: a
terminal, a browser, a folder in the file viewer, another chat, an app you built.

`system/scripts/layout.py` is the agent-facing helper. Use it whenever you want
to surface, inspect, or arrange windows. Do not hand-edit the shell's state
files.

> **Where the script lives:** `layout.py` is at the **repo root**, at
> `system/scripts/layout.py` (i.e. `/home/user/workspace/system/scripts/layout.py`,
> the container WORKDIR). It is **NOT** inside this skill's folder. Every
> command below is written as `python3 system/scripts/layout.py ...`, a path
> relative to the repo root, which is the cwd for all commands in this repo.

## The vocabulary (read this first)

| Word | Meaning |
|---|---|
| **desktop** | A named, shared collection of windows and shortcuts over a wallpaper. Everyone sees the same desktops and the same windows on them. |
| **window** | One page of one app on one desktop: the app, the path under the app's origin the page is at (`chat` at `/?chat=<id>`, `terminal` at `/?session=<name>`, `files` at `/notes/`), and the title the page last reported. Named by an id, `win-<hex>`. |
| **placement** | Where one *client* keeps one window on its screen: its frame, whether it is snapped or maximized, whether it is minimized. Per client, never shared. |
| **client** | One browser (its windows share it). Each client has one active desktop and its own placements of every desktop. |
| **launch path** | A path an app declares for opening a new page of itself (`chat` and `terminal` declare `new` at `/new`; `files` declares `new` at `/` with a `path` parameter). An app that declares none offers `open` at `/`. |
| **shortcut** | An icon on a desktop's backdrop that runs one app's launch path, in `focus` mode (raise the app's most recent window, opening one only when it has none) or `new` mode (always open one). |

**Shared vs per client.** Desktops, their windows, their shortcuts, and their
wallpaper are shared: an `open` or a `close` is seen by everyone. Placements
(`focus`, `place`, `minimize`, `maximize`, `restore`) and which desktop is
active belong to one client.

## Which client an op targets

Every op targets exactly one client. With no `--client`, that is the client
that most recently messaged you, else the one connected client. When neither
settles it (several clients, an agent nobody messaged), the op is refused with
the connected clients listed; pass `--client <id>` (from `context`). Ops are
never applied to every client at once.

- **An op with no `--desktop` edits the client's active desktop.** That is what
  you want nearly always; just run the op.
- **Pass `--desktop <name>` to edit a different desktop.** The op edits that
  desktop and switches the client to it, so the user sees what you arranged.
- **`context` tells you which client asked**: every known client with its
  active desktop, connection state, and last few messages. The client that
  most recently messaged you is almost always the requester.
- **`load <desktop>` switches a client onto a desktop** without changing anything
  else (`load "Research"`).

## Naming a window

A window argument is one of:

- a **window id** (`win-0123456789abcdef`), from `desktops` or printed by the
  `open` that made it;
- **`self`**, your own chat's window (the chat app's window whose path carries
  `$MINDS_CHAT_ID`, or `$MNGR_AGENT_ID` for an agent that is its own chat);
- an **app name** (`files`, `browser`), that app's most recently focused window
  on the target client's active desktop.

## The verbs you'll use 95% of the time

| Goal | Command |
|---|---|
| See which client asked for something | `python3 system/scripts/layout.py context` |
| List every desktop, its windows (id, app, path, title), and every client | `python3 system/scripts/layout.py desktops` |
| List every app with its launch paths and windows | `python3 system/scripts/layout.py list` |
| Switch a client onto a desktop | `python3 system/scripts/layout.py load <desktop> [--client <id>]` |
| Open an app (at its default launch path) | `python3 system/scripts/layout.py open terminal` |
| Open a specific page of an app | `python3 system/scripts/layout.py open files --path /notes/` |
| Open a page with launch parameters | `python3 system/scripts/layout.py open terminal --launch new --param workdir=/data` |
| Open a web page in a new browser | `python3 system/scripts/layout.py open https://example.com` |
| Bring a window to the front | `python3 system/scripts/layout.py focus <window>` |
| Close a window | `python3 system/scripts/layout.py close <window>` |

`open` prints the window's id (the new one's, or the focused one's) to **stdout**
so you can name it in later ops. It opens the window at `--path`, or at a launch path (`--launch <id>` with
`--param name=value` for its parameters; with neither, the app's default launch
path). A window of the app already at that path is focused rather than
duplicated; pass `--if-present new` to open another. The window lands on the
target client's active desktop (or `--desktop`), on top of that client's stack.

Some paths worth knowing: your own chat is `open chat --path
"/?chat=${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"`; a folder is `open files --path
/notes/` (dufs serves `data/` at `/`, so `/notes/` is `data/notes`; the `path`
launch parameter, `open files --param path=/notes/`, lands in the same folder
but as a window at `/?path=/notes/`, and a window is focused only when its
path matches exactly, so use one form per folder); a browser is `open browser
--path "/?session=<name>"`; a terminal is `open terminal --path
"/?session=<name>"`.

## Arranging windows

All of these take the same `--client` and `--desktop` as `open`. They edit the
target client's placements only:

| Goal | Command |
|---|---|
| Snap a window to a half of the screen, or maximize it | `python3 system/scripts/layout.py place <window> --zone left\|right\|maximized` |
| Put a window at an exact frame (fractions of the backdrop) | `python3 system/scripts/layout.py place <window> --frame 0.05,0.05,0.6,0.7` |
| Minimize / restore / maximize | `python3 system/scripts/layout.py minimize <window>` / `restore <window>` / `maximize <window>` |
| Point a window at another path under its app | `python3 system/scripts/layout.py navigate <window> /other/path` |
| Reload one window's page, or every page of an app | `python3 system/scripts/layout.py refresh <window>` / `refresh --app <name>` |

`navigate` and `refresh` reach the page itself: `navigate` sets the window's
path as if its page had reported it (the client's page follows), and `refresh`
reloads the page (on the target client, or on every client for `--app`).

The most common natural request, "put a terminal next to my chat", is:

```bash
python3 system/scripts/layout.py place self --zone left
python3 system/scripts/layout.py place "$(python3 system/scripts/layout.py open terminal)" --zone right
```

## Shortcuts and the wallpaper

Each desktop's backdrop carries **shortcuts**: one per (app, launch path), in
grid cells. A new desktop is seeded with every app's `default_shortcut` from
its manifest.

```bash
# The target client's active desktop's shortcuts: app, launch path, mode, cell.
python3 system/scripts/layout.py shortcuts

# Add the docs app's "open" to Research's backdrop, always opening anew, in column 2 row 0.
python3 system/scripts/layout.py shortcut set docs open --mode new --cell 2,0 --desktop "Research"

# Move it, or take it off.
python3 system/scripts/layout.py shortcut move docs open --cell 3,0 --desktop "Research"
python3 system/scripts/layout.py shortcut remove docs open --desktop "Research"

# The wallpaper: a bundled image (`dawn` ships; GET /api/wallpapers lists what does), a file
# under data/.apps/system_interface/wallpapers/, or none.
python3 system/scripts/layout.py wallpaper bundled dawn --desktop "Research"
python3 system/scripts/layout.py wallpaper none
```

`--desktop` switches the target client onto that desktop for every op, `shortcuts`
included, so to look at another desktop's shortcuts without moving anyone, read them
off `desktops`, which lists every desktop's shortcuts.

`shortcut set` refuses an app or launch path the registry does not declare.
Creating, renaming, or deleting a *desktop itself* has no `layout.py`
subcommand: it is the taskbar's desktop menu, or the shell's REST routes
(`POST /api/desktops`, `POST /api/desktops/<id>/settings`, `POST
/api/desktops/<id>/delete`), which the `system_interface` README documents.

## Inspecting state

`desktops` prints every desktop with its windows (`id`, `app`, `path`, `title`,
`is_settling`: true from an open at a launch path until the page's first
location report) and shortcuts, and every client with its `active_desktop`,
`is_connected`, and `shown` (the windows of its active desktop it has not
minimized). `list` prints every app with its launch paths, whether it is
running, and where its windows are, plus the same desktops and clients. Both
print JSON.

When the user names a window by its title, find its id with `desktops` (the
row whose `title` matches) rather than guessing.

## What is not here, and what to use instead

The shell knows windows and pages, not what backs them. What an app's page
stands for (a chat's agent, a terminal's tmux session, a browser's Chromium) is
the app's own to name, stop, or end:

| Goal | Where it lives |
|---|---|
| Retitle a chat / a terminal | the chat's own route: `POST <chat url>/api/chats/<id>/rename`; the terminal offers no rename route yet |
| Stop or start what backs a page | the app's own route (the chat's stop route; the browser's `POST /browsers/<name>/stop` and `.../start`) |
| End a chat, a terminal, a browser | the app's own route (the chat's destroy route, the browser's `DELETE /browsers/<name>`); the terminal offers no delete route yet, so end its tmux session from a shell; `close` only closes the window |
| Open another chat | the chat root page (`open chat`), or `open chat --path "/?chat=<id>"` |

`layout.py` refuses the old tabbed-shell verbs (`split`, `move`, `rename`,
`delete`, `stop`, `start`, `replace-url`, `inspect`, `where`, `views`) and the
old `app:`, `chat:`, `chat-terminal:`, `terminal:`, `service:`, `url:`, and
`subagent:` spellings with the verb or form to use instead.

## Ops answer at once

The shell edits the files itself and answers with the result, so every op
returns as soon as the file is written; a connected window shows it within a
redraw. Ops print a one-line description on **stderr** (`opened window
win-... (terminal at /new) on desktop home for client ...`, `placed window ...
in the left zone ...`); `refresh` prints `(sent refresh to client <id>)`.

**stdout** is reserved for machine-readable output: the id of the window
`open` made, the structured output of the read commands, and the desktop's
shortcuts as they stand after a `shortcut set`, `shortcut move`, or
`shortcut remove`.

## Exit codes

- `0` ok (including no-op successes)
- `1` error (the specific reason is in stderr, including "could not tell
  which client this op is for", for which you pass `--client <id>` from
  `context`, and a window that no desktop holds)
- `3` the shell cannot do it right now (a 409 or a 503: a save in flight, an
  app still starting up): retry after a short backoff, or tell the user

## When NOT to use this skill

- **Building a brand-new app.** Use `build-app` to scaffold it first; it
  ends with a `layout.py open` call to surface the new window.
- **Persisting layout state.** The frontend auto-saves every gesture.
