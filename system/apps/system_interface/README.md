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
--reinstall`), rebuilds the gitignored `static/` bundle and broadcasts a
`reload_system_interface` op (frontend), and/or restarts the services agent so
the editable backend re-imports the merged `.py` (backend). For a backend change
it pre-flights the merged code on a throwaway port before touching the live
service, then polls the loopback endpoint to confirm health. If anything fails,
it restores the tree to `--rollback-to` as a forward revert commit, rebuilds and
restarts from it, and re-confirms the UI is healthy -- so the served interface
can never be left broken. The exit code reports the outcome (`0` revealed, `2`
rolled back, `3` emergency, `1` precondition error).

The `reload_system_interface` op it broadcasts goes to the loopback-only
`/api/layout/broadcast` endpoint, which relays a `layout_op` WebSocket message;
the dockview shell (`DockviewWorkspace.ts`) reloads the top-level page -- shell
chrome plus every child chat iframe -- so the browser picks up the new hashed
assets. This is distinct from `system/scripts/layout.py refresh`, which only reloads a
single inner iframe/panel for arranging the workspace.

## Projects

Tabs are grouped into *projects*. A project is a named set of tabs plus
the arrangement they are in, so switching projects swaps the whole tab
set. Membership is implicit -- a tab is in a project exactly when a
panel for it exists in that project's saved content -- so there is no
separate membership map to keep in sync.

Each project is one JSON file under the primary agent's
`workspace_layout/projects/` directory, with a `projects_meta.json`
registry (per-project name, color, and glyph, plus the last-active id).
A project with no saved content renders as the fresh welcome-chat
state. The `everything` project always exists and cannot be deleted:
every new tab is appended to its stored content as well as the active
project's, which is what makes it the unfiltered view while still
letting it keep its own arrangement.

Each browser client remembers its active project in localStorage
(`si-active-project-id`) and reopens it on the next connect, falling
back to `everything` and then to the first project when the stored one
is gone. Autosaves target the active project; when one client saves,
deletes, or restyles a project, clients with it open re-apply it live
and a deletion moves them onto the fallback project. The REST surface
is `GET /api/projects`, `POST /api/projects` (create, server-side
slugification of the name into the id), `GET|POST /api/projects/<id>`
(read and autosave), `POST /api/projects/<id>/settings`,
`POST /api/projects/<id>/delete`, and
`POST /api/projects/panels/<panel_id>/delete` (drop one panel from
every project that holds it).

The switcher sits at the left of a bar across the top of the workspace:
the active project's squiggle -- one of the ten glyphs in
`frontend/src/views/squiggles.ts` -- its name, and a menu of every
project, plus "New project..." and a per-project settings dialog (name,
color, glyph, and delete). Down the left edge is a 37px icon rail that
expands on hover, headed by the active project, with quick-add rows for
a chat, file viewer, browser, or terminal tab and a row per app running
on the machine. Everything the rail opens lands in the active project;
unlike the "+" menu it does not hide an app that already has a tab, so
one app can appear in several projects at once.

Every tab carries the same controls: Refresh (reloads what the tab is
showing -- service-wide for a service-backed iframe, the transcript and
stream for a chat), Open in new window (disabled on chats and subagent
views, which have no address of their own), Share, Destroy, and Close.
Close drops the tab from the project on screen; Destroy is
confirm-gated, tears down whatever backs the tab, and removes it from
*every* project, including ones no client currently has open. A
destroyed chat's transcript stays accessible.

Chat messages sent through the UI (and every project switch) are logged
to `workspace_layout/events/client_activity/events.jsonl` with the
sending client's id, device kind, and active project, so agents can
attribute a request to a client via `layout.py context`.

The named layouts that projects replace still exist on disk
(`workspace_layout/layouts/`, `GET /api/layouts` and friends), but no
client keeps one active any more and the "+" menu no longer exposes
them. The agent-facing layout ops below therefore resolve their
`--layout` against the projects registry whenever the name is not one
of the named layouts, which is how an agent addresses the arrangement a
client is actually looking at.

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
# to the foreground). Mutating ops always name their target, which is
# normally the project the client is in (``context`` reports it).
python3 system/scripts/layout.py open web --layout Everything

# Reload one tab (or, for ``service:<name>``, every iframe tied to
# that service).
python3 system/scripts/layout.py refresh web

# Inspect the grid tree -- arrangements, sizes, active panel,
# ref-resolved panel list -- of the named project or layout.
python3 system/scripts/layout.py inspect --layout Everything
```

Every op POSTs `{op, args, agent_id}` to the loopback-only
`/api/layout/broadcast` endpoint on the system interface. Mutating ops
require a target, are delivered only to connected clients that have it
active (HTTP 412 when there are none -- the error lists each connected
client and what it is on), and acquire an in-process advisory mutex (HTTP
409 with the in-flight holder's metadata on contention); reads bypass
both. Panels are addressed by stable, type-prefixed refs:
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
