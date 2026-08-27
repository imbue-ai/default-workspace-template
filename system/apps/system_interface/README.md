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
once approved, applied. See
`.agents/skills/update-system-interface/SKILL.md`.

`reveal_system_interface.py` owns the deterministic preview setup/teardown, as
sub-commands:

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

Going live, after approval, is the general **update apply** -- shared with the
`update-self` flow:

```bash
python3 .agents/skills/update-self/scripts/update_self.py apply \
    --merge-ref "mngr/update-<slug>" \
    --worker-bundle "<work_dir>/system/apps/system_interface/imbue/system_interface/static"
```

It merges the worker's branch (capturing the rollback point internally),
classifies what changed and does only what is needed: refreshes dependencies
if a manifest changed (`npm ci`, plus the vendored mngr tool, the backend tool
and the workspace venv -- the same environments `build_workspace.sh` builds),
installs the worker's already-built `static/` bundle (live build as fallback),
and/or pre-flights the merged code on a throwaway port before restarting the
services agent so the editable backend re-imports the merged `.py` (backend).
It then polls the loopback endpoint to confirm health and checks that the
frontend really serves, and only after those does it ask every open view of the
workspace to reload -- unconditionally, since a backend-only change leaves the
open page rendering what it had already fetched, but last, so an apply that
regressed the frontend rolls back instead of asking every open view to reload
into it. If anything fails, it reverts the entire merge as a forward revert
commit, restores the pre-apply snapshots, restarts only if the failed apply had
already restarted the service, and re-confirms the UI is healthy -- so the
served interface can never be left broken. The exit code reports the outcome
(`0` applied, `2` rolled back, `3` emergency, `1` precondition error). A
persistent marker under `data/.state/update-apply/` makes even a hard kill
mid-apply recoverable (`update_self.py recover`, run automatically at boot and
from a recovery cron).

Two properties are load-bearing there. It **snapshots `static/` (and the
affected environments) before anything destructive runs**, because the
destructive steps delete before they produce (`npm ci` removes `node_modules`;
the build empties the bundle directory; the env refreshes rebuild the venv and
tool environments) -- so a rollback restores a *copy* rather than re-running
the build that just failed, and a broken build environment cannot take the UI
down with it. And it **checks that the frontend actually serves**, not just
that the backend answers: the "not built" placeholder and an unserved
`/assets` path are both HTTP 200s to `/api/agents`, so the probe confirms the
app shell is the real app and that its module script comes back as JavaScript.

The apply's reload of every open view is delegated to
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
(`system/scripts/build_workspace.sh`) and by the apply above. Nothing rebuilds
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
what the apply's frontend probe reads. And `/assets/<path>` is registered
unconditionally rather than only when the bundle exists at startup: a route
decided at construction time can never notice a bundle that appears later, and
without it asset requests fall through to the SPA catch-all and come back as
`index.html` with a `text/html` type, which the browser refuses as a module
script -- a blank screen instead of the placeholder. A genuinely missing asset
gets a plain 404.

## When the served code is behind the tree

A missing bundle is the loud version of a more general problem: an update lands
by advancing the working tree, and this process only becomes consistent with it
once it restarts into the merged code. The apply does both in one motion, but
an interrupted apply, a failed apply whose rollback could not restore health,
or a hand merge outside the flow can leave a live server rendering old code
over new on-disk state -- silently, which is the shape the geebspace incident
took.

So the server says so. It records the tree HEAD it started from and, when the
live tree has moved *in a way that affects what this process runs*, injects a
`system-interface-update-staleness` meta tag into the built app shell, from
which the frontend renders one dismissible informational line. Three values, checked in
this order:

- `update-emergency` when the apply's emergency record
  (`data/.state/update-apply/emergency.json`) is present -- a rollback that
  could not put a healthy workspace back. It outranks the other two because it
  is the one state here that does not resolve itself, and the one neither of
  them can see: that exit clears the marker, and its rollback has already put
  the tree content back, so both would read as consistent.
- `update-interrupted` when the apply's marker
  (`data/.state/update-apply/marker.json`) is present.
- `updated-not-activated` otherwise.

"Affects what this process runs" is the whole design (see `update_staleness.py`
for the rules and their test table). A bare HEAD comparison would show the
banner near-permanently -- minds commit their ordinary work in this repo
constantly, the apply's own version-history commit lands after the restart, and
a frontend-only apply rebuilds the served bundle without restarting -- so the
check diffs the startup HEAD against the current one and reports only when a
changed path is backend code this process imports, a manifest its environment
was resolved from, the vendored mngr, or `.mngr/settings.toml`. The banner
informs only; acting on it stays with the agent.

## Projects

The workspace shows one _view_ at a time: a project, or Everything. The
machine holds a single pool of objects -- chat agents, terminal
sessions, browsers, and registered apps' numbered instances -- and a
project is a filter over that pool plus its own dockview arrangement.
Membership is an explicit list of member refs (`chat:<agent-id>`,
`terminal:<name>`, `service:<name>?instance=<name>-<N>` for an app
instance, `service:<name>` for an app's pin,
`service:browser?session=<name>`) kept separately from the layout, and
it is many-to-many: the same object can be in any number of projects at
once, nothing owns anything, and there is no "move". An
ad-hoc URL page is _not_ a member: it has no identity beyond the panel
showing it, so it is only ever a tab in one view's arrangement. (A
machine that migrated from before projects may still carry
`url:<hash>` members, filed by the migration from panels it could not
otherwise name; such a leftover is removed from its rail row like any
other member, which is the only verb it has.)
A member with no panel is _backgrounded_ -- still running, still listed
in the rail -- so closing a tab never stops the underlying object or
changes membership. "Remove from project" hides an object in that one
view only; only the destructive per-kind verbs (below) actually end
something, and they take it out of every project at once.

Everything is the unfiltered view, and the home. It is not a project --
it has no registry entry and no member list, and cannot be renamed or
deleted -- but it keeps its own arrangement like any other view. Its
tab list enumerates the machine, so an object in no project at all
still appears there.

There is one live page per object, machine-wide: an app instance open
in three projects is one iframe and one document, not three. Switching
views, closing a tab, or re-arranging panes never reloads or duplicates
anything; a page is torn down only when the object behind it is
destroyed.

A plain app is multi-instance the way terminals and browsers are: each
open pane is a numbered instance ("File Viewer 2", ref
`service:files?instance=files-2`) minted by the backend (lowest free
number machine-wide, `POST /api/apps/<name>/instances/allocate`).
Instance existence is derived rather than stored: an instance exists
while any project's member list or any view's saved layout references
it, and removing the last reference is its deletion (`GET
/api/apps/instances` lists what exists). Opening an app -- its rail
shortcut in focus mode, its All apps popover row, a pinned `service:`
ref -- goes to the view's most recently used instance and mints the
first one when the view shows none; "new"-mode shortcuts, the
launcher's tiles, and `layout.py open <app> --new` always mint. An
instance that beacons its location (the file viewer does; the
`build-app` scaffold includes the one-liner) reopens at the path it was
showing: the page posts `{type: "minds-location", path}` one hop up on
each load, the shell validates the origin against its own service
origins, resolves the pane by its window, and stores the path by ref
(`GET|POST /api/member-locations`), cleared when the instance is
deleted.

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
collides with another agent's. Terminals, browsers, and app instances
derive their display names from their identities ("Terminal 3" from the
`terminal-3` tmux session, "Browser 1" from the daemon-minted
`browser-1`, "File Viewer 2" from the allocator-minted `files-2` --
with the app's own chosen name riding into its instances' names, so a
"Docs" app's instances read "Docs 2") and have no rename gesture; an agent's `layout.py rename` for those kinds writes
the machine-wide title registry
(`workspace_layout/member_titles.json`), which every surface reads
before falling back to the derived name. Last-used timestamps are keyed
by member ref in `member_last_used.json` the same way, so recency ranks
the same in every launcher. Destroying an object drops its name and
recency with it.

Each browser client remembers its active view in localStorage
(`si-active-project-id`) and reopens it on the next connect, falling
back to the first project when the stored one is gone -- and to
Everything when there is no project to fall back to, since a machine may
hold none. Autosaves target
the active view; project, membership, title, and last-used changes
broadcast over the WebSocket so every connected client catches up live,
and a deletion moves clients sitting in the deleted project onto the
fallback. The REST surface is `GET /api/projects`, `POST /api/projects`
(create, server-side slugification of the name into the id),
`GET|POST /api/projects/<id>` (read and autosave),
`POST /api/projects/<id>/settings`, `POST /api/projects/<id>/delete`,
`POST /api/projects/<id>/members` and
`POST /api/projects/<id>/members/remove`,
`POST /api/projects/<id>/shortcuts` (record one shortcut's pin or mode
override -- body `{shortcut, is_pinned?, mode?}`, where `shortcut` is a
built-in name or `app:<service>`; the response carries the project's
full sparse `shortcut_overrides` map),
`POST /api/projects/members/share` (add one member to several projects),
`GET /api/projects/members`, `POST /api/projects/panels/<panel_id>/delete`
(drop one panel from every project that holds it),
`GET|POST /api/member-titles`, `GET|POST /api/member-last-used`,
`GET|POST /api/member-locations` (the location-beacon store),
`GET /api/apps/instances` (the derived instance inventory), and
`POST /api/apps/<name>/instances/allocate` (mint the next free
instance name).

Down the left edge is a 37px project rail that expands on hover to
float over the dock. Top to bottom: the active view's squiggle -- one
of the ten glyphs in `frontend/src/views/squiggles.ts` -- and name (the
row opens the view switcher, with "New project" and Everything;
right-clicking it opens project settings: name, color, glyph, and
delete -- deleting a project removes the view and nothing else: every
object it showed keeps running and stays in Everything, and a machine
may sit at zero projects. Settings is display metadata and the delete,
nothing more: taking an object out of a project is a verb on the object,
so it lives on the object's own row); shortcut rows for Chat, File
Viewer, Browser, and Terminal (File Viewer renders disabled until an app
backs it). Every shortcut -- built-in and pinned app alike -- has a
per-project **mode**, which its label reads from: focus mode ("Chat")
goes to the view's most recently used member of that kind (per the
machine-wide recency store) and creates only when the view shows none,
while new mode ("New Chat") always creates. Defaults are code-side --
chat defaults to new, everything else to focus -- and every project is
still created with one chat of its own so the user lands in a working
chat. Each shortcut row also carries its own kebab/right-click menu: the
complementary action ("New X" in focus mode, "Focus last X" in new mode,
disabled when the view shows no X), the mode flip, and Unpin (built-ins
only); a pinned app's row combines its object verbs, a divider, and that
shortcut group. Shortcut-initiated creates take the same in-flight guard
the launcher tiles have, so a second click cannot start a second object.
Each built-in row carries the same pin a pinned app does, and unpinning
one moves it into the "All apps" popover for that project alone -- the
overrides are stored sparsely (`shortcut_overrides` on the project's
registry entry), so a project that has never touched this shows all four
on the defaults, and Everything, which has no project entry to record
against, always does.
Then the project's pinned apps and that "All apps" popover -- pinning an
app in a project _is_ its membership, so the popover toggles the app's
bare `service:<name>` member ref and nothing else. It lists only apps
the view has _not_ pinned, since the pinned ones are already in the rail
a few pixels away; a just-pinned row fades out rather than vanishing,
and a pinned row carries its own pin icon so unpinning stays one click.
Everything's rail instead shows a fixed shortcut row for every openable
app -- built-ins first in rail order, then apps alphabetically, no
unpin (there is no registry entry to record one against) -- so a newly
added app appears there automatically, and its All apps popover shows
the already-pinned empty state; a search pill that filters rows by
label and kind; and the view's tab list, open members as primary text
and backgrounded ones as tertiary, each row with a hover kebab -- or a
right-click -- offering the verbs for its kind ("Remove from project"
included; only a menu-less legacy `url:` row keeps a one-click remove
instead).
The tab list holds app INSTANCES ("File Viewer 2"), never bare apps: a
zero-instance app lives in the rail, the popover and the launcher tiles
but has no tab-list row. A pinned app whose shortcut row is drawn above
is not repeated in the list; its own menu hangs off the shortcut
instead. The rail and the dock tab build their menus from the _same_
definition (`frontend/src/views/objectMenu.ts`), so which verbs an
object offers, and the order they read in, are settled in one place for
both.

New tabs come from a full-page New Tab launcher that opens as a real
tab: tiles to start a chat, browser, or terminal from scratch, an "In
this project" table of the view's members, and an "On this machine"
table of everything else, each with a kind-filter menu and a
last-active column ordered by the machine-wide recency store. Opening a
row from the machine table _adds_ it to the project on screen; nothing
leaves the projects it was already in. The dock never goes empty --
closing the last tab opens a launcher -- and a launcher folds up on its
own once another panel takes focus.

Every tab carries an X that closes the tab and nothing else, sitting
outboard of a menu built from the same per-kind definition the rail
row's is. Refresh reloads what
the object is showing -- service-wide for a service-backed iframe, the
transcript and stream for a chat, a reattach for a terminal, whose tmux
session outlives the panel and keeps its scrollback across one. Share is
an app affordance, since the share surface is per registered service.
Rename is offered for a chat and nothing else. A chat is an mngr agent:
its ref is a stable agent id and `mngr rename` moves the name everywhere
the agent is known, so the name the user gives it is the name anything
else -- an agent included -- can address it by. A terminal and a browser
have no identity apart from their names (a live tmux session, a Chromium
profile directory), so a rename there could only be a display name over
the top. An app does have a stable id in its registered service name, but
that name is also the only handle anything else accepts -- `layout.py`
expands a bare word to `service:<word>` -- so a renamed app could be read
and not addressed. All three keep their registered or derived names. Hide
tab closes the panel and preserves membership, and is the tab menu's
alone; "Remove from project" is the rail row menu's alone. Putting a tab
away is what you want while looking at the tab, and taking an object out
of a project is what you want while looking at the project's list of what
it shows. Delete <name> is the confirm-gated destructive verb for a
chat, a terminal, a browser session, and an app instance: one act with
one wording. It tears the object off the machine, so it leaves _every_
project, including ones no client currently has open; a destroyed
chat's transcript stays accessible, and a deleted app instance's
service keeps running -- only the instance's references (memberships,
panes, name, recency, stored location) go, which is all an instance is.
Delete is withheld for the primary agent, which runs the workspace's
own services. An instance's menu also carries the SERVICE's own verbs
in a trailing group -- Share, and the reversible Stop <app> (no
confirmation -- it is one click from undone), which stops the app's
supervisord program via `POST /api/apps/<name>/stop`; a stopped app's
menus offer Start <app> (`POST /api/apps/<name>/start`) in its place.
Both are idempotent, neither touches the registry row or any
membership, and both are offered only for an app whose registry entry
carries a `program` (see `forward_port.py --program`); the essential
services (`system_interface`, `terminal`) are refused by name. The
registry row is the app's identity and whether it runs is derived:
`AgentManager` probes supervisord's process state for `program` rows
(and TCP-connects the registered URL otherwise), carries the result as
`AppEntry.is_running` on the apps WebSocket, and every surface renders
a stopped app's rows dimmed, with its open tabs showing a minimal
stopped placeholder (with a Start button) instead of a dead iframe.
Clicking a stopped app anywhere starts it first and opens its pane.
Instances of an app the machine no longer offers stay listed, with
Delete as the remaining verb. Real removal (delete the supervisord
block, the package, the registry row) is the mind's job via the
`update-app` skill; the `POST /api/apps/<name>/deregister` endpoint
remains for agent tooling but no UI surface calls it.

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

This compiles the frontend into `imbue/system_interface/static/`. The
`postbuild` step stamps the output with `git rev-parse HEAD:./` -- the hash of
the *committed* frontend tree, not of the files just built. A build from a
frontend tree with uncommitted changes is stamped as its last commit, so the
update apply's stamp check (which compares it against the merged tree) cannot
tell that bundle from one built at that commit. Commit before building a
bundle that will be handed to the apply.
