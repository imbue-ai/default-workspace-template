# System Interface

The workspace's shell: its window manager and app management. It serves one
document (`/`, the dockview UI) that arranges tabs, keeps projects, and
manages apps, and it knows every app only through the workspace app model: a
manifest, a registry row, an instances API, and the browser-side contract.
This package imports nothing from mngr or from any app, never runs the `mngr`
binary, and names no app (`test_project_ratchets.py` holds all three).

## Model

The shell speaks the vocabulary of the workspace app model. The meta spec,
`docs/system/blueprint/workspace-app-model/plan-workspace-app-model.md`, is
the reference; `contracts.md` beside it holds every route, message, and file
format. In brief:

- An **app** is a supervised program with a manifest (`app.toml`), a row in
  the registry (`data/.state/apps.toml`), and its own browser origin. It is the
  unit you install, stop, start, and share.
- An **instance** is something an app owns, lists, and reports status for:
  a chat, a terminal session, a file viewer, a browser. An app that declares
  none has exactly one, itself. Every tab shows an instance, and an
  **address** (`app:<name>` or `app:<name>?instance=<key>`) is the whole of
  what the shell knows about what a tab shows.
- A **view** is a project or Everything. A **project** is a shared tab set (a
  list of addresses), a name, a color, a glyph, and its rail **shortcuts**
  (`(app, action)` rows in focus or new mode); Everything is the unfiltered
  view of the whole machine.
- A **layout** is one client's arrangement of one view. A **client** is one
  browser context, identified by a stored id, with a device kind. Truth is
  shared, arrangement is scoped.
- **Status** (`working`, `idle`, `attention`, `stopped`, `error`), titles,
  icons, and recency all come from the apps. The shell stores no titles, no
  recency, and no locations of its own.

## What the process serves

The `system-interface` tool (the shell's own uv tool environment, run by
supervisord from the repo root) listens on `http://127.0.0.1:8000` and serves:

- `/` and the SPA catch-all: the dockview UI, built into
  `imbue/system_interface/static/`; `/assets/<path>` for its bundle.
- `/api/health`: `{"status", "is_frontend_built"}`, the probe the update
  apply and the preview flow poll.
- `/_static/app_contract.js`: the browser-side contract module every app page
  imports (source in `system/libs/workspace_ui/src/app_contract.ts`).
- The shell routes of contracts sections 5 and 6: the app registry and the
  per-app Stop and Start (`/api/apps/<name>/...`), the instance relay
  (`.../instances`, `.../instances/<key>/rename|delete|location|stop|start`),
  projects (`/api/projects/...`), layouts (`/api/layouts/<view>`), clients,
  tabs (`/api/tabs/<tab_id>/instance`), client activity, the inventory
  (`/api/inventory`), and the loopback-only op route (`/api/layout/broadcast`).
- The WebSocket (`/api/ws`): `apps_updated`, `projects_updated`,
  `layout_updated`, `active_view_changed`, `tab_rebound`, `layout_op`
  (contracts section 8).

Its state lives under `data/.state/system_interface/`: `projects.json`,
`layouts/<view>/<client>.json` with a per-device seed beside each,
`clients.json`, the migration marker, and the client-activity event log
(`events/client_activity/events.jsonl`, what `layout.py context` reads).

The backend is the `imbue/system_interface/shell/` subpackage (inventory,
relay, projects, layouts, clients, client activity, layout ops, the pure
dockview document editor, routes, state); the package root holds the process
(`main.py`, `server.py`), the not-built placeholder, and the update-staleness
check. The frontend (`frontend/`) is one member of the npm workspace rooted at
`system/package.json`; the design system, the base helpers, and the contract
modules it shares with the app pages live in `system/libs/workspace_ui`, and
`src/relay.ts` is the shell's side of the embedder relay (it forwards the
framed pages' `minds:` messages to the minds chrome unchanged).

### How the shell learns about apps

The **inventory** (`shell/inventory.py`) watches the registry, probes each
app's liveness (supervisord for rows with a `program`, a TCP connect
otherwise), fetches each app's instance list from its instances API, refetches
on the app's nudge (`POST /api/apps/<name>/changed`, coalesced over a short
window) and on a periodic sweep, and pushes the diffed result to every browser
as `apps_updated`.

Every verb an instance accepts goes through the **relay** to the app that owns
it, so the tab menu and the rail row offer exactly what the record allows
(`renameable`, `stoppable` read with the record's status; the app's
`instances`, `program`, and `critical` flags), from one definition
(`frontend/src/views/tabMenu.ts`). Stop and Start of the whole app act on its
supervisord program and are refused for critical apps; a single-instance app
offers them on its tab, every other app on the rail's per-app row menu
(`frontend/src/views/Sidebar.ts`). A framed page reaches the shell only
through the contract module (`shell:open`, `shell:focused`, `shell:location`);
a page that reports the path it is showing gets it stored on its own record
and reopens there.

### Views, layouts, and the New Tab page

Every open in a project files the address into its tab set, whichever way it
was opened; "Remove from project" unfiles it. Each client keeps its own
arrangement of each view: the browser saves the user's gestures with a save
id and the stamp it was based on (a save over a newer arrangement is refused
with 409 and the window refetches), the shell writes the file for agent ops
and its own pruning, and every write is announced as `layout_updated` so the
client's other windows mirror it. The active view lives on the client record.
A saved tab whose address the machine no longer lists is pruned on the next
observation; an instance whose record says `lifetime = "referenced"` is
deleted through its app once nothing references it.

The rail shows the view's identity (the switcher; right-click for project
settings), its shortcut rows (seeded from every app's `default_shortcut`;
Everything's rail is every app's primary action), the "All apps" popover, a
search pill, and the view's tab list. The New Tab page is
the only empty state, and the page for starting things: a search field; "Open
new" (every app's primary action as a tile, the apps that declare a
`launcher_rank` in their manifest leading in rank order, the rest on a second
row); "In this project" (the tab set, with an app filter and a last-active
column, omitted when empty); "Start something" (hardcoded intents, each a new
chat seeded with a prompt, six at a time behind "See more"); and "Start from a
template" (the published templates by category, in sideways rails, with a
detail dialog whose "Make it mine" starts a chat that adopts the template).
Typing in the search field swaps the page for results: the machine's
instances and actions, the matching intents, the matching templates. A seeded
prompt goes to whichever app declares an action with a `message` param (the
chat app's `new`), so the shell still names no app. The template catalog is a
JSON document the shell fetches from `SYSTEM_INTERFACE_TEMPLATE_CATALOG_URL`
(`catalog/README.md` at the repo root describes it), reuses for six hours,
keeps the last good copy under `data/.state/system_interface/`, and serves
to the page at `GET /api/templates-catalog`; the design is
`docs/system/blueprint/new-tab-page/plan-new-tab-page.md`. A fresh install lands
there with no project.

A workspace that predates the app model is carried over once by
`system/scripts/migrate_workspace_layouts.py`, which bootstrap runs at every
boot behind its marker and the update apply runs before its restart; `plan
--json` shows what a run would write.

## Running and developing

```bash
# Backend, from the repo root (the registry path and the state directory
# default are relative to it; run elsewhere, the shell finds no registry and
# lists no apps)
uv run system-interface

# Frontend (with hot reload)
cd system/apps/system_interface/frontend
npm install
npm run dev

# Build the bundle into imbue/system_interface/static/
npm run build
```

The build's `postbuild` step stamps the output with three `git rev-parse`
tree hashes (this frontend directory, the shared `system/libs/workspace_ui`,
and the workspace's `package-lock.json`), each as *committed*, not as the
files just built. The update apply compares that stamp against the merged
tree, so commit before building a bundle that will be handed to the apply.

The frontend styles in the markup: Tailwind utilities over a semantic token
layer (`system/libs/workspace_ui/src/base.css`), with shared primitives
(Button, Modal, the input and badge recipes) for repeated looks. Prefer them
when extending the default look; see [`frontend/style_guide.md`](frontend/style_guide.md).
It is a convention, not a rule: a user who wants their interface restyled gets
that, tokens or not.

## Driving the workspace layout from an agent

An agent inside the workspace rearranges the dockview through
`system/scripts/layout.py` (`list / inspect / where / context / views / load /
open / focus / split / close / move / rename / delete / stop / start /
maximize / restore / replace-url / refresh / shortcuts / shortcut set /
shortcut remove`), which speaks addresses:

```bash
python3 system/scripts/layout.py list
python3 system/scripts/layout.py context
python3 system/scripts/layout.py open app:files?instance=files-2 --view Everything
python3 system/scripts/layout.py open terminal
python3 system/scripts/layout.py rename app:terminal?instance=terminal-3 "Build log"
python3 system/scripts/layout.py inspect --view Everything
```

The document ops (`open`, `focus`, `split`, `close`, `move`) are applied by
the shell to the target client's layout file and announced as
`layout_updated`, so an op lands whether or not a browser is connected. Every
op targets exactly one client (`--client <id>`, else the client that last
messaged the requesting agent, else the one connected client; refused with the
clients listed otherwise); `--view` edits that view and switches the client to
it; `open` of an app with instances creates one through the relay inside the
op (`--action`, `--param`; a bare URL is the browser's `new`). Only
`maximize`, `restore`, `refresh`, and the interface reload reach the browser
as messages. See the `manage-layout` skill for end-to-end orientation.

## Updating the running UI

The deployed system interface is the live web UI the user is looking at, so
changes are not applied in place. The canonical flow is the
`update-system-interface` agent skill: a change is delegated to a worker,
tested in isolation, **previewed** to the user as a tab
(`reveal_system_interface.py preview --slug <name> --work-dir <dir>` boots the
worker's already-built work_dir on a free port and registers it, with a
labeled wrapper page, as the `si-preview` app; `unpreview` tears it down),
and, once approved, applied through the general **update apply** shared with
the `update-self` flow:

```bash
python3 .agents/skills/update-self/scripts/update_self.py apply \
    --merge-ref "mngr/update-<slug>" \
    --worker-bundle "system_interface=<work_dir>/system/apps/system_interface/imbue/system_interface/static" \
    --worker-bundle "chat=<work_dir>/system/apps/chat/imbue/chat/static"
```

The apply merges the worker's branch, classifies what changed and does only
what is needed (a dependency refresh, the worker's already-built bundles or a
live build, a pre-flight boot of the merged shell and chat on throwaway
ports), restarts the services agent, then probes: the shell's `/api/health`,
the instances API of every critical app that serves one (at the manifest's
`instances_url` when it declares one, else the app's registered URL), and that
the frontend really serves (the "not built" placeholder and an unserved
`/assets` path are both HTTP 200, so the probe reads the `X-Frontend-Built`
header and checks that the module script comes back as JavaScript). Only then
does it ask every open view to reload, through
`system/scripts/refresh_workspace_view.py` (a `reload_system_interface` op on
the loopback-only op route, which reloads the top-level page and every child
frame, plus the minds app's own refresh endpoint). On any failure it reverts
the merge as a forward revert commit, restores the pre-apply snapshots it took
before anything destructive ran, and re-confirms health; the exit code reports
the outcome (`0` applied, `2` rolled back, `3` emergency, `1` precondition).
The scripts under `.agents/skills/update-self/scripts/` and that skill's
`SKILL.md` are the reference.

## When the bundle is missing

`static/` is gitignored build output, produced at workspace creation
(`system/scripts/build_workspace.sh`) and by the apply above. Nothing rebuilds
it at service start, so a code refresh that replaces the tree can leave the
backend with nothing to serve. In that state `/` serves a placeholder, and
because the placeholder is a string in the backend rather than part of the
bundle, it still works when nothing else does.

The placeholder is the workspace's general recovery surface, so it hands over a
**terminal** rather than a repair. It embeds the already-running terminal app
in a frame, and suggests creating an agent to do the work if the reader would
rather not:

```
env -u TMUX mngr create --connect --template chat --label user_created=true --message "i'm seeing \"this workspace's interface needs to be rebuilt, can you fix it?\""
```

`--template chat` is what makes the result a chat, and it carries everything
that is not a choice: the shared work directory, the output style, and running
in the workspace tree rather than a worktree of it. It is harness-agnostic --
`output_style` is honored by the claude, codex and pi plugins alike -- so it
neither picks a harness nor can be relied on to.

The rest is where the line departs from what the chat app's
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
agents. Nothing is spawned either way: the terminal app is supervised, always
running, and sits at a *lower* (more protected) memory band than this server,
so the page points at something that outlives it.

The terminal's origin label is minted per workspace, so the page cannot carry
it; the server reads it from the app registry (`data/.state/apps.toml`) at
render time and the page's own script derives the origin from the browser's
location, mirroring `system/libs/workspace_ui/src/origin.ts`. When there is no
terminal registered -- the terminal app starts alongside the other apps, not
before them -- the frame stays hidden and the prose stands alone.

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
was resolved from, or the vendored mngr. The banner
informs only; acting on it stays with the agent.

