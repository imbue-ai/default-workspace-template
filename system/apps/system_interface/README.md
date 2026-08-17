# System Interface

Web chat interface for viewing and interacting with mngr-managed Claude agents.

Shows live conversations from Claude session files in a web UI, with real-time
updates via Server-Sent Events.

## Usage

```bash
system-interface
```

Opens at http://127.0.0.1:8000 by default.

## Development

```bash
# Backend
cd system/apps/system_interface
uv run system-interface

# Frontend (with hot reload)
cd system/apps/system_interface/frontend
npm install
npm run dev
```

## Updating the running UI (canonical flow)

The deployed system interface is the live web UI the user is looking at, so
changes are not applied in place. The canonical flow is the
`update-system-interface` agent skill: a change is delegated to a worker, tested
in isolation (including Playwright against an isolated instance) and run through
the review gates; then **previewed** to the user as a tab before merging; and,
once approved, merged and revealed. See
`.agents/skills/update-system-interface/SKILL.md`.

The same `reveal_system_interface.py` script owns the deterministic setup/teardown
on both sides of that user gate, as sub-commands:

- `preview --slug <name> --work-dir <worker-work-dir>` boots the worker's
  already-built work_dir (a local worktree-agent folder in this same container)
  on a free port and registers it as the `si-preview-app` service, then boots a
  small wrapper page that embeds it in a labeled "preview" frame and registers
  that as the user-facing `si-preview` service -- so the proxied tab reads as a
  clearly-marked proposed change rather than a nested clone of the live UI. No
  fetch, no re-checkout, no rebuild, and without merging or touching the served
  tree. (Resolve the work_dir from
  `mngr ls --include 'name=="<name>"' --format json` -> `agents[0].work_dir`.)
- `unpreview --slug <name>` tears that down -- kill both servers, deregister both
  services (idempotent).
- `reveal --rollback-to <sha>` reveals the merged change (below).

The reveal, after merge, is a single self-healing command. With the known-good
revision captured before the merge (`ROLLBACK_TO=$(git rev-parse HEAD)`):

```bash
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py reveal --rollback-to "$ROLLBACK_TO"
```

It classifies what changed and does only what is needed: refreshes dependencies
if a manifest changed (`npm ci` / `uv tool install -e system/apps/system_interface
--reinstall`), rebuilds the gitignored `static/` bundle (frontend), and/or
restarts the services agent so the editable backend re-imports the merged `.py`
(backend), then asks every open view of the workspace to reload --
unconditionally, since a backend-only change leaves the open page rendering what
it had already fetched. For a backend change it pre-flights the merged code on a
throwaway port before touching the live service, then polls the loopback
endpoint to confirm health. If anything fails,
it restores the tree to `--rollback-to` as a forward revert commit, rebuilds and
restarts from it, and re-confirms the UI is healthy -- so the served interface
can never be left broken. The exit code reports the outcome (`0` revealed, `2`
rolled back, `3` emergency, `1` precondition error).

That reload is delegated to `system/scripts/refresh_workspace_view.py`, the shared
helper every flow that restarts the services agent uses. It fires two channels,
because neither reaches every viewer: a `reload_system_interface` op, and the Minds
app's own refresh endpoint (which lands even when the page's WebSocket never came
back from the restart).

The `reload_system_interface` op goes to the loopback-only
`/api/layout/broadcast` endpoint, which relays a `layout_op` WebSocket message;
the dockview shell (`DockviewWorkspace.ts`) reloads the top-level page -- shell
chrome plus every child chat iframe -- so the browser picks up the new hashed
assets. That reaches every attached browser, including anyone the workspace was
shared with over a Cloudflare tunnel. This is distinct from
`system/scripts/layout.py refresh`, which only reloads a single inner
iframe/panel for arranging the workspace.

## Projects

The workspace shows one *view* at a time: a project, or Everything. The
machine holds a single pool of objects -- chat agents, terminal
sessions, browsers, registered apps, and ad-hoc URL pages -- and a
project is a filter over that pool plus its own dockview arrangement.
Membership is an explicit list of member refs (`chat:<agent-id>`,
`terminal:<name>`, `service:<name>`, `service:browser?session=<name>`,
`url:<hash>`) kept separately from the layout, and it is many-to-many:
the same object can be in any number of projects at once, nothing owns
anything, and there is no "move". A member with no panel is
*backgrounded* -- still running, still listed in the rail -- so closing
a tab never stops the underlying object or changes membership. "Remove
from project" hides an object in that one view only; only the
destructive per-kind verbs (below) actually end something, and they
take it out of every project at once.

Everything is the unfiltered view, and the home. It is not a project --
it has no registry entry and no member list, and cannot be renamed or
deleted -- but it keeps its own arrangement like any other view. Its
tab list enumerates the machine, so an object in no project at all
still appears there.

There is one live page per object, machine-wide: an app open in three
projects is one iframe and one document, not three. Switching views,
closing a tab, or re-arranging panes never reloads or duplicates
anything; a page is torn down only when the object behind it is
destroyed.

Each project is one JSON file under the primary agent's
`workspace_layout/projects/` directory, with a `projects_meta.json`
registry (per-project name, color, glyph, and member list, plus the
last-active id). A project with no saved content renders as the New Tab
launcher. A machine upgrading from before projects folds its old
`desktop` arrangement into one starter project ("Project 1") with each
panel filed as a member, so nothing moves and nothing is lost.

Names and last-used timestamps belong to the object, not to any one
view: both are keyed by member ref in their own small registries
(`workspace_layout/member_titles.json` and `member_last_used.json`), so
every surface calls an object the same thing and recency ranks the same
in every launcher. New chats, terminals, and browsers are named
automatically -- "Chat 1", "Terminal 2", and so on, taking the lowest
free number -- and nothing asks for a name; double-clicking a tab's
title (or its menu's Rename) renames the object everywhere it appears.
Destroying an object drops its name and recency with it.

Each browser client remembers its active view in localStorage
(`si-active-project-id`) and reopens it on the next connect, falling
back to the first project when the stored one is gone. Autosaves target
the active view; project, membership, title, and last-used changes
broadcast over the WebSocket so every connected client catches up live,
and a deletion moves clients sitting in the deleted project onto the
fallback. The REST surface is `GET /api/projects`, `POST /api/projects`
(create, server-side slugification of the name into the id),
`GET|POST /api/projects/<id>` (read and autosave),
`POST /api/projects/<id>/settings`, `POST /api/projects/<id>/delete`,
`POST /api/projects/<id>/members` and
`POST /api/projects/<id>/members/remove`,
`POST /api/projects/members/share` (add one member to several projects),
`GET /api/projects/members`, `POST /api/projects/panels/<panel_id>/delete`
(drop one panel from every project that holds it),
`GET|POST /api/member-titles`, and `GET|POST /api/member-last-used`.

Down the left edge is a 37px project rail that expands on hover to
float over the dock. Top to bottom: the active view's squiggle -- one
of the ten glyphs in `frontend/src/views/squiggles.ts` -- and name (the
row opens the view switcher, with "New project" and Everything;
right-clicking it opens project settings: name, color, glyph, and
delete); shortcut rows for Chat, File Viewer, Browser, and Terminal,
which go to what the view already shows and create only when it shows
none (every project is created with one chat of its own, and its Chat
shortcut goes to that chat; File Viewer renders disabled until an app
backs it); the project's pinned apps and an "All apps" popover --
pinning an app in a project *is* its membership, so the popover's
"Pinned in <project>" and "Unpinned" halves toggle the app's member ref
and nothing else, and Everything pins nothing because it already lists
every app; a search pill that filters rows by label and kind; and the
view's tab list, open members as primary text and backgrounded ones as
tertiary, each row with a hover kebab offering the verbs for its kind
(Remove from project, Share app, Delete from this machine for chats,
terminals, and browsers).

New tabs come from a full-page New Tab launcher that opens as a real
tab: tiles to start a chat, browser, or terminal from scratch, an "In
this project" table of the view's members, and an "On this machine"
table of everything else, each with a kind-filter menu and a
last-active column ordered by the machine-wide recency store. Opening a
row from the machine table *adds* it to the project on screen; nothing
leaves the projects it was already in. The dock never goes empty --
closing the last tab opens a launcher -- and a launcher folds up on its
own once another panel takes focus.

Every tab carries a minus that closes the tab and nothing else, plus a
menu offering Refresh (reloads what the tab is showing -- service-wide
for a service-backed iframe, the transcript and stream for a chat;
terminals have none), Share for app tabs, Rename, Close tab, and one
confirm-gated destructive verb per kind: Shut down agent, Shut down
terminal, Shut down browser, or Unregister app. The shut-downs tear
down the object itself, so it leaves *every* project, including ones no
client currently has open; a destroyed chat's transcript stays
accessible. Unregister app is deliberately weaker: it removes the app
from the registry (`POST /api/apps/<name>/deregister`) and from every
project, but nothing in the workspace supervises the program answering
on the port, so the program keeps running.

Chat messages sent through the UI (and every view switch) are logged
to `workspace_layout/events/client_activity/events.jsonl` with the
sending client's id, device kind, and active view, so agents can
attribute a request to a client via `layout.py context`.

The named-layout store that projects replace is retired: its API is
gone and only the on-disk files remain (`workspace_layout/layouts/`,
read once as the migration source). The agent-facing layout ops below
resolve their `--view` against the projects registry (including
`Everything`), and an op naming no view goes to the one the connected
client is looking at. A view is arranged per device -- desktop and
mobile clients each save their own arrangement of the same view,
sharing its members -- and the read ops take `--device` to pick which
arrangement to read (default desktop).

## Driving the workspace layout from an agent

An agent running inside the workspace container can rearrange the
dockview through the agent-facing `system/scripts/layout.py` helper. The
subcommand surface covers `list / inspect / where / context / load /
open / focus / split / close / move / rename / maximize / restore /
replace-url / refresh`.

```bash
# Print every addressable thing (registered services + mngr agents)
# with open/running flags. YAML by default, ``--json`` to switch.
python3 system/scripts/layout.py list

# See which browser clients exist, their device kind, current project,
# and recent messages (to attribute a request to a client/project).
python3 system/scripts/layout.py context

# Surface the given service in a tab split alongside the primary chat
# (reports a no-op if one is already open; use ``focus`` to bring it
# to the foreground). With no ``--view``, the op goes to the view the
# connected client is looking at; name one to address another view.
python3 system/scripts/layout.py open web --view Everything

# Reload one tab (or, for ``service:<name>``, every iframe tied to
# that service).
python3 system/scripts/layout.py refresh web

# Inspect the grid tree -- arrangements, sizes, active panel,
# ref-resolved panel list -- of the named view (a project, or
# ``Everything``); ``--device mobile`` reads its mobile arrangement.
python3 system/scripts/layout.py inspect --view Everything
```

Every op POSTs `{op, args, agent_id}` to the loopback-only
`/api/layout/broadcast` endpoint on the system interface. Mutating ops
target a view (the connected client's own when unnamed), are delivered
only to connected clients that have it active (HTTP 412 when there are
none -- the error lists each connected client and what it is on), and
acquire an in-process advisory mutex (HTTP 409 with the in-flight
holder's metadata on contention); reads bypass both. Panels are addressed by stable, type-prefixed refs:
`service:<name>`, `chat:<agent-name>`, `subagent:<session-id>`,
`terminal:<short-hash>`, `url:<short-hash>`. Subcommands that take a
"service or ref" argument also accept a bare service name (e.g. `web`
-> `service:web`). See the `manage-layout` skill for end-to-end
orientation.

## Building

```bash
cd system/apps/system_interface/frontend
npm run build
```

This compiles the frontend into `imbue/system_interface/static/`.
