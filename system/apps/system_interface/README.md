# System Interface

The workspace's shell (its window manager and app management) and, until phase
10 of the workspace app model, the chat app it embeds: live conversations with
mngr-managed agents, with real-time updates via Server-Sent Events.

## Two documents from one process

The process serves two documents. The shell document (`/`, the dockview UI)
arranges tabs, keeps projects, and manages apps. The chat document
(`/<agent-id>`, and `/<agent-id>.<session-id>` for a subagent view) is one
page per chat, which the shell frames as an ordinary app tab at the
registered `chat` origin; its manifest is `system/apps/chat/app.toml`, and the
shell's supervisord line registers that row beside its own at the same port.
Requests are dispatched to the chat app by path (`wsgi_dispatch.py`), never
by origin label, so loopback callers such as `curl
http://127.0.0.1:8000/api/agents/<id>/events` keep working. The chat app
serves the instances API of the workspace app model at `/_instances`
(`chat_instances.py`, over the agent manager) and the presence route its
pages report through; the shell serves `/api/health` and the browser-side
contract module at `/_static/app_contract.js`. The frontend is two entries of
one vite build: `index.html` (the shell) and `chat.html` (`src/chat/`, which
the shell bundle never imports), plus the contract library
(`vite.contract.config.ts`). The chat page talks to the shell only through
that contract (`src/app_contract.ts`; the shell's side is `src/relay.ts`,
which also relays the chat pages' permission cards to the minds chrome).

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

## The shell: apps, instances, projects, and views

The shell is a generic window manager over the workspace app model
(`docs/system/blueprint/workspace-app-model/contracts.md`). It knows nothing
about chats, terminals, files, or browsers by name: every one is an **app**
with a manifest (`app.toml`), a row in the registry (`data/.state/apps.toml`),
and an instances API (`GET /_instances` at the app's URL) that lists the
app's **instances** with their titles, statuses, and what verbs they accept.
The backend for all of this is the `imbue/system_interface/shell/`
subpackage; the chat modules stay at the package root until phase 10.

Everything is addressed as `app:<name>` (a single-instance app, or the app
itself) or `app:<name>?instance=<key>` (one instance). The shell keeps an
**inventory** (`shell/inventory.py`): it watches the registry, probes each
app's liveness (supervisord for rows with a `program`, a TCP connect
otherwise), fetches each app's instance list, refetches on the app's nudge
(`POST /api/apps/<name>/changed`, coalesced) and on a periodic sweep, and
pushes the diffed result to every browser as `apps_updated`. Titles, status
dots, icons, and recency all come from those records; there are no
shell-side name, recency, or location stores any more.

The workspace shows one **view** at a time: a project, or Everything. A
project is a shared **tab set** (a list of addresses) plus its rail
**shortcuts**, kept in `data/.state/system_interface/projects.json`;
Everything is the unfiltered view of the whole machine and is not a project.
Every open in a project files the address into its tab set, whichever way it
was opened (a launcher row, a rail shortcut, a layout op, a page's own
`shell:open`); "Remove from project" unfiles it and nothing else. Each client
keeps its own arrangement of each view (`layouts/<view>/<client>.json`, with
a per-device seed beside it) and autosaves it; a saved tab whose address the
machine no longer lists is pruned on the next observation. An instance whose
record says `lifetime = "referenced"` is deleted through its app once nothing
references it any more. First landing after a fresh install is the New Tab
page; there is no starter project until phase 9's migration creates one.

Verbs go through the shell's **relay** to the app that owns the instance:
create (`POST /api/apps/<name>/instances`), rename, delete, and the location
report (`.../instances/<key>/rename|delete|location`), so the tab menu and
the rail row offer exactly what the record allows (`renameable`, the app's
`instances`, `program`, and `critical` flags), from one definition
(`frontend/src/views/tabMenu.ts`). Stop and Start (`POST
/api/apps/<name>/stop|start`) act on the app's supervisord program and are
refused for critical apps. A framed page reaches the shell only through the
contract module (`shell:open`, `shell:focused`, `shell:location`, ...); an
app that reports the path it is showing gets it stored on its own record and
reopens there.

The rail down the left edge shows the view's identity (the switcher, and
right-click for project settings), its shortcut rows (a project's stored
shortcuts, seeded from every app's `default_shortcut`; Everything's rail is
every app's primary action), the "All apps" popover (pin an action to the
project's rail), a search pill, and the view's tab list. The New Tab page is
the only empty state: tiles for every app's primary action, "In this
project" (the tab set), and "On this machine" (everything else), each with an
app filter and a last-active column.

Until phase 10 the chat app keeps two marked exceptions in the shell: its
`new` action takes the provider account picked beside its launcher tile (and
opens the chooser when nothing is signed in), and the shell's WebSocket still
carries `agents_updated` and the proto-agent messages for the chat pages.
Chat sends and view switches are logged to
`data/.state/system_interface/events/client_activity/events.jsonl` so
agents can attribute a request to a client via `layout.py context`.

## Driving the workspace layout from an agent

An agent running inside the workspace container can rearrange the dockview
through `system/scripts/layout.py`. The subcommand surface is `list / inspect
/ where / context / views / load / open / focus / split / close / move /
rename / delete / maximize / restore / replace-url / refresh / shortcuts /
shortcut set / shortcut remove`.

```bash
# Every app with its instances, statuses, and which clients dock each.
python3 system/scripts/layout.py list

# Which browser clients exist, their device kind, current view, and recent
# messages (to attribute a request to a client and a view).
python3 system/scripts/layout.py context

# Open an instance in the view the connected client is looking at, or name
# the view. A bare app with instances creates a fresh one and prints the
# new address to stdout.
python3 system/scripts/layout.py open app:files?instance=files-2 --view Everything
python3 system/scripts/layout.py open terminal

# Retitle or delete an instance through its app.
python3 system/scripts/layout.py rename app:terminal?instance=terminal-3 "Build log"
python3 system/scripts/layout.py delete app:terminal?instance=terminal-3

# Inspect the grid tree of a view: arrangements, sizes, the active panel.
python3 system/scripts/layout.py inspect --view Everything
```

The dock ops POST `{op, args, agent_id}` to the loopback-only
`/api/layout/broadcast` endpoint. Mutating ops target a view (the connected
client's own when unnamed), are delivered only to connected clients that have
it active (HTTP 412 when there are none), and acquire an in-process advisory
mutex (HTTP 409 on contention); reads bypass both. `rename`, `delete`, and
`replace-url` call the relay routes under `/api/apps/<name>/instances/<key>/`
and the `shortcut` subcommands the project routes, so they take no view and no
mutex. A bare word is
`app:<word>`; the old spellings (`chat:`, `terminal:`, `service:`, `url:`,
`subagent:`) are refused with an error naming the new form. See the
`manage-layout` skill for end-to-end orientation.

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
