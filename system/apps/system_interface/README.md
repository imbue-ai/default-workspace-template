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
if a manifest changed (`npm ci`, plus the vendored mngr tool, the backend tool
and the workspace venv -- the same environments `build_workspace.sh` builds),
rebuilds the gitignored `static/` bundle (frontend), and/or
pre-flights the merged code on a throwaway port before restarting the services
agent so the editable backend re-imports the merged `.py` (backend). It then
polls the loopback endpoint to confirm health and checks that the frontend
really serves, and only after those does it ask every open view of the workspace
to reload -- unconditionally, since a backend-only change leaves the open page
rendering what it had already fetched, but last, so a reveal that regressed the
frontend rolls back instead of asking every open view to reload into it. If
anything fails, it restores the tree to `--rollback-to` as a forward revert
commit, restores the bundle, restarts only if the failed reveal had already
restarted the service, and re-confirms the UI is healthy -- so the served
interface can never be left broken. The exit code reports the outcome (`0`
revealed, `2` rolled back, `3` emergency, `1` precondition error).

Two properties are load-bearing there. It **snapshots `static/` before anything
destructive runs**, because both steps delete before they produce (`npm ci`
removes `node_modules`; the build empties the bundle directory) -- so a rollback
restores a *copy* rather than re-running the build that just failed, and a broken
build environment cannot take the UI down with it. And it **checks that the
frontend actually serves**, not just that the backend answers: the "not built"
placeholder and an unserved `/assets` path are both HTTP 200s to `/api/agents`,
so the probe confirms the app shell is the real app and that its module script
comes back as JavaScript.

The reveal's reload of every open view is delegated to
`system/scripts/refresh_workspace_view.py`, the shared
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

## When the bundle is missing

`static/` is gitignored build output, produced at workspace creation
(`system/scripts/build_workspace.sh`) and by the reveal above. Nothing rebuilds
it at service start, so a code refresh that replaces the tree can leave the
backend with nothing to serve. In that state `/` serves a placeholder, and
because the placeholder is a string in the backend rather than part of the
bundle, it still works when nothing else does.

The placeholder is the workspace's general recovery surface, so it hands over a
**terminal** rather than a repair. It embeds the already-running `terminal`
service (ttyd) in a frame, and suggests creating an agent to do the work if the
reader would rather not:

```
env -u TMUX mngr create --connect --template chat --label user_created=true --message "i'm seeing \"this workspace's interface needs to be rebuilt, can you fix it?\""
```

`--template chat` is what makes the result a chat, and it carries everything
that is not a choice: the shared work directory, the output style, and running
in the workspace tree rather than a worktree of it. It is harness-agnostic --
`output_style` is honored by the claude, codex and pi plugins alike -- so it
neither picks a harness nor can be relied on to.

The rest is where the line departs from what
`agent_manager._build_chat_create_command` passes for the same chat, in four
places:

- **`--connect`, against its `--no-connect`.** Someone typing this wants to land
  in the conversation. It is load-bearing rather than decorative: this
  workspace's own `[commands.create] connect = false` is the default it
  overrides.
- **No `--type`, which the builder must pass.** The app is serving a harness the
  user picked from a menu; this page has no such choice to carry, so hardcoding
  one would hand a codex or pi workspace a line that quietly opens claude.
  Omitted, mngr resolves it from `[commands.create] type`.
- **No `--transfer none`, which the builder spells out.** The `chat` template
  already sets it. Unlike the harness this is not the reader's to choose -- an
  agent in a worktree would repair a copy of the workspace instead of the
  workspace -- so a test reads the template and fails if that setting ever
  leaves it.
- **A `--message` carrying the page's own heading**, so the agent opens already
  knowing what the reader is looking at. A test ties the two together, since a
  reworded heading would otherwise leave the message quoting a sentence that
  appears nowhere.

The line also names no agent, so mngr mints one and a second run starts a fresh
conversation rather than colliding with the first. `env -u TMUX` is what lets the
connect half work from the workspace's tmux-backed terminal tabs, which `mngr
connect` otherwise refuses to attach from.

Tests pin the whole of it: the flags against the builder, the line parsed back
into an argv and resolved against the live mngr CLI, the same line word-split by
a real `sh` (because `shlex` expands nothing and a shell does), and the rendered
page's own repair block -- so the suggestion cannot drift into creating something
that is not a chat, into a line that does not run, or into a line other than the
one a reader copies.

A shell rather than a "rebuild" button because a button has to be right about
what went wrong: the states that strand a workspace here are dominated by ones
where a build dispatched from the server would fail too (no registry, no
memory, a lockfile that does not resolve), and it would fail with nowhere to
report it, on a page with no application to render the failure. It would also
inherit the server's memory band and be protected ahead of the user's chats and
agents. Nothing is spawned either way: ttyd is supervised, always running, and
sits at a *lower* (more protected) memory band than this server, so the page
points at something that outlives it.

The terminal's origin label is minted per workspace, so the page cannot carry
it; the server reads it from the app registry (`data/.state/apps.toml`) at
render time and the page's own script derives the origin from the browser's
location, mirroring `frontend/src/origin.ts`. When there is no terminal
registered -- ttyd starts alongside the other services, not before them -- the
frame stays hidden and the prose stands alone.

The page returns to the interface on its own once a bundle exists, so a
rollback (or a build run in that terminal) needs no further action. It polls the
`X-Frontend-Built` header rather than reloading on a timer: a whole-page refresh
would destroy the terminal session every few seconds, right while it is being
typed into. The timer-based reload survives only inside `<noscript>`, where
there is no terminal to protect.

Two things make that state recoverable rather than terminal. Every app-shell
response carries an `X-Frontend-Built` header, so the placeholder is
distinguishable from the real app without pattern-matching its markup -- that is
what the reveal's frontend probe reads. And `/assets/<path>` is registered
unconditionally rather than only when the bundle exists at startup: a route
decided at construction time can never notice a bundle that appears later, and
without it asset requests fall through to the SPA catch-all and come back as
`index.html` with a `text/html` type, which the browser refuses as a module
script -- a blank screen instead of the placeholder. A genuinely missing asset
gets a plain 404.

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

Each view keeps one arrangement file per device kind under the primary
agent's `workspace_layout/projects/` directory -- `<id>.json` for
desktop, `<id>.mobile.json` for mobile -- with a `projects_meta.json`
registry (per-project name, color, glyph, and member list, plus the
last-active id). Membership is shared across devices; only tab
placement differs, and each client loads and autosaves its own UA-derived
kind's file. A view with no saved content on this device renders as the
New Tab launcher. A machine upgrading from before projects folds its old
`desktop` arrangement into one starter project ("Project 1") with each
panel filed as a member -- and its old `mobile` layout into that
project's mobile arrangement -- so nothing moves and nothing is lost.

Names and last-used timestamps belong to the object, not to any one
view. Every object is named the way the minds app names hosts: a
human-readable display name paired with a canonical true name that is a
deterministic transform of it. New chats, terminals, and browsers are
named automatically -- "Chat 1", "Terminal 2", "Browser 1", taking the
lowest free number -- and nothing asks for a name. A chat's display
name lives on its mngr agent (its `display_name` label, whose canonical
form -- `Chat-1` -- is the agent's mngr name), so `mngr list` and the
tab always agree; renaming a chat (double-click its tab title, or its
tab menu's Rename, or `layout.py rename`) goes through `mngr rename`
and keeps the pair matched, refusing a name whose canonical form
collides with another agent's. Terminals and browsers derive their
display names from their identities ("Terminal 3" from the `terminal-3`
tmux session, "Browser 1" from the daemon-minted `browser-1`) and have
no rename gesture; an agent's `layout.py rename` for those kinds writes
the machine-wide title registry
(`workspace_layout/member_titles.json`), which every surface reads
before falling back to the derived name. Last-used timestamps are keyed
by member ref in `member_last_used.json` the same way, so recency ranks
the same in every launcher. Destroying an object drops its name and
recency with it.

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
terminals have none), Share for app tabs, Rename for chats (the one
kind whose name is chosen rather than derived), Close tab, and one
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

# List the views themselves: every project plus Everything, with members,
# per-device content presence, and which clients are on each.
python3 system/scripts/layout.py views

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
