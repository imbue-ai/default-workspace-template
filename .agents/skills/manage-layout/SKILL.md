---
name: manage-layout
description: Use when you want to rearrange the workspace dock tabs (open, split, move, focus, close, maximize, reload, rename or delete an instance) or inspect the live layout.
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
| `app:<name>` | A single-instance app's one tab; or, as an `open` / `split` target, "a fresh instance of this app". | `app:files`, `open app:terminal` |

A bare word is shorthand for `app:<word>` (`open files`). The literal `self`
resolves to your own chat panel; most useful as `--relative-to=self` on
`split` / `move`. Your own chat's address is `app:chat?instance=$MNGR_AGENT_ID`.

The old `chat:` / `terminal:` / `service:` / `url:` / `subagent:` spellings are
refused by name, with the address to use instead. `layout.py list` prints
every address on the machine, with each instance's title and status, so you
never have to guess: find the row whose title the user said, and use its
address.

External `https://` URLs cannot be opened as tabs yet (that lands in phase 8
of the workspace app model); open them in a browser instance instead.

## Views

The workspace shows one *view* at a time: a **project** (a shared set of tabs
plus its own arrangement) or **Everything** (every instance on the machine).
Each connected browser client has exactly one view active, and every change
that client makes auto-saves into its own arrangement of that view.

- **An op with no target goes to the view the connected client is looking
  at.** That is what you want nearly always; just run the op.
- **Pass `--view <name>` to address a different view** (a project's name, or
  `Everything`). The op applies only on connected clients that have that
  view active; with none, it fails fast with an error listing what each
  client is on.
- **`views` lists the views**: every project plus Everything, each with its
  tab set and which connected clients have it in front.
- **`context` tells you which client asked**: every known client with its
  device kind, active view, connection state, and last few messages. The
  client that most recently messaged you is almost always the requester.
- **`load <view>` switches a client onto a view** so you can then mutate it
  (`load "Research"`, then run your ops). By default it targets the client
  that most recently messaged you; pass `--client <id>` (from `context`) to
  pick one explicitly.

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
| Put a new terminal in the same tab group as your chat | `python3 system/scripts/layout.py split terminal --relative-to=self --direction=within` |
| Close a tab | `python3 system/scripts/layout.py close <address>` |

`open` is the opinionated default. It puts the new tab to the right of your
chat, joining whatever group already lives there if one is open. Pass
`--new-group` to force a fresh column instead.

What `open` does with each target:

- `open app:files` (a single-instance app): docks its one tab, or reports a
  no-op if it is already open (use `focus` to bring it to the front).
- `open app:terminal?instance=terminal-2` (an instance address): docks that
  instance, or reports a no-op if it is already open.
- `open terminal` (a bare app that has instances): runs the app's action and
  creates a **fresh** instance every time, exactly like the rail's "New
  terminal". The new instance's address is printed to **stdout** so you can
  capture it for later ops. The same holds for `open chat` (a new chat) and
  `open browser` (a new browser).

A terminal created this way starts in the workspace root. Pass a different
directory by creating it through the terminal app's own action (the rail's
menu) or by `cd`-ing in the shell.

## Less common operations

All of these take the same optional `--view <name>` as `open` (except
`refresh`, which reloads iframes on every client):

| Goal | Command |
|---|---|
| Place a new panel with explicit positioning | `python3 system/scripts/layout.py split <address> --relative-to=<address> --direction=<left\|right\|above\|below\|within> [--ratio=0.4] [--new-group]` |
| Focus an existing tab | `python3 system/scripts/layout.py focus <address>` |
| Move an open tab next to / into another's group | `python3 system/scripts/layout.py move <address> --relative-to=<address> --direction=<dir> [--new-group]` |
| Maximize / restore a group | `python3 system/scripts/layout.py maximize <address>` / `python3 system/scripts/layout.py restore` |
| Reload one tab (or every iframe of an app) | `python3 system/scripts/layout.py refresh <address>` |

And three verbs that go through the app that owns the instance rather than
the dock (they take an instance address, never a bare app):

| Goal | Command |
|---|---|
| Retitle an instance (the title shows in every view) | `python3 system/scripts/layout.py rename <address> "<title>"` |
| Delete an instance (it leaves every view) | `python3 system/scripts/layout.py delete <address>` |
| Point an instance at a path under its app | `python3 system/scripts/layout.py replace-url <address> /<path>` |

Not every app accepts every verb: a terminal's title is its own, and an app
that does not track locations refuses `replace-url`. The app's refusal is
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
  [app:files*] size=0.6
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

## Mutating ops are synchronous

Every dock op (`open`, `split`, `move`, `focus`, `close`, `maximize`,
`restore`, `refresh`) waits for the resulting state to be observable via
`inspect` before returning. On success it prints a one-line diff on
**stderr** (`opened app:files in tabs=[...]`, `created
app:terminal?instance=terminal-3 in tabs=[...]`, `moved ... into ...`). On a
**no-op** it prints `no change: <address> is already ...` and exits 0.
`maximize`, `restore`, and `refresh` change nothing `inspect` can see, so they
print `(broadcast sent; no observable layout-state change to confirm)`.

**stdout** is reserved for machine-readable output: the address of an
instance `open` / `split` created, and the structured output of the read
commands. Diffs and no-op messages always go to stderr.

## Exit codes

- `0` ok (including no-op successes)
- `1` error (the specific reason is in stderr, including the wait-stable
  timeout and the "no connected client has that view active" rejection; for
  the latter, `load` the view first or ask the user to switch)
- `3` mutex conflict: another agent's layout op is in flight (retry after a
  short backoff; the stderr message includes the in-flight holder's
  `agent_id`, `op`, `args`, `started_at`, and a suggested `retry_after_ms`)

## When NOT to use this skill

- **Building a brand-new app.** Use `build-app` to scaffold it first; it
  ends with a `layout.py open` call to surface the new tab.
- **Projects themselves** (what a project shows, its rail shortcuts, adding
  a tab to a project without opening it): see `manage-projects`.
- **Persisting layout state.** The frontend auto-saves on every change.
