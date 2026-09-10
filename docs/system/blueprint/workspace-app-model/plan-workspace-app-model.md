# The workspace app model

This is the meta spec for the arc that turns the workspace into an operating system of web apps and moves chat out of the system interface into an app of its own.
It replaces the earlier `split-chat-apart` plan that lived in the mngr repo, which is deleted; that plan kept mngr and harness knowledge in the shell and froze the per-kind addressing schemes, both of which this spec reverses.
It is written for the people and agents implementing the arc, and it is the reference the implementation is judged against.
Implementation happens in this repository, in one pull request built as the ordered phases at the end of this document.
Its glossary and model are the vocabulary the per-phase specs beside it use.
The exact schemas, routes, messages, and file formats live in [contracts.md](contracts.md); the per-phase files `phase_01_*.md` to `phase_11_*.md` name the files, tests, and manual checks of each commit; [mngr_side_changes.md](mngr_side_changes.md) covers the paired mngr branch.

## 1. Purpose and principles

A minds workspace is an operating system whose programs are web servers.
The system interface is its window manager: it arranges tabs, keeps projects, and manages apps, and it knows nothing about what any app shows.
Everything that concerns chats and agents, including the whole of mngr, lives inside the chat app, so the chat app can one day be replaced by another agent harness without touching the shell.

Five principles decide every question below:

1. **One owner per fact.** Existence of an instance belongs to its app. Arrangement belongs to a client. Membership belongs to a project. Nothing is recorded in two places.
2. **The shell is generic.** It has no code path for chats, terminals, browsers, or files. Every built-in app goes through the same contract a user-built app does.
3. **Truth is shared, arrangement is scoped.** What runs and what exists is the same for every viewer of a workspace. How it is laid out belongs to one client.
4. **Minimal shell state.** The shell stores the app registry, the project registry, and client layouts. Titles, locations, status, and recency of instances come from apps.
5. **No two-phase commits.** Verbs are owned by exactly one side, and the other side reacts to observed truth.

## 2. Glossary

Code and documentation use these names.

| Term | Meaning | Retired alternatives |
|---|---|---|
| App | A supervisord program with a registered origin, a manifest, and an icon. The unit you install, stop, start, and share. | service, program |
| Instance | Something an app owns, lists, and reports status for, with an app-scoped key and a current URL. Every app has at least one; an app that declares no instances has exactly one, the app itself. | session, resource, object, member |
| Tab | The rendering of one instance in one client's layout. Not a model concept; dockview's panel is the implementation. | pane, panel |
| View | A project, or Everything. A shared set of instance addresses plus each client's arrangement of them. | space, desktop |
| Project | A named view with a color, a glyph, a tab set, and shortcuts. | |
| Everything | The unfiltered view whose tab set is every instance of every app. Not a project. | |
| Layout | One client's arrangement of one view: which of its instances are docked and where. | arrangement |
| Client | One connected browser context, identified by a stored id, with a device kind. | viewer, device |
| Shortcut | A per-project rail entry `(app, action)` in focus or new mode. | pin, launcher tile |
| Action | A way to open an app that the app declares in its manifest, such as `new`. | |
| Address | `app:<name>` or `app:<name>?instance=<key>`, the one way to name an instance. | ref |
| Shell | The system interface: window manager plus app management. Its registered app name is `system_interface`. | chrome, desktop |
| Status | One of `working`, `idle`, `attention`, `stopped`, `error`, reported per instance by its app. | activity state, liveness |
| Manifest | `app.toml` beside an app's code: its static declarations. | |
| Registry | `data/.state/apps.toml`: the runtime record of registered apps, written by `forward_port.py`. | |

Terms retired outright: member, backgrounded, object, kind, chat-terminal, pending tab, and the launcher-versus-rail distinction between "open new" and shortcuts.
An instance in a project's tab set that a client has not docked is described in exactly those words; it has no name of its own.

## 3. The model

### 3.1 Apps

An app is one `[program:*]` entry in `system/supervisord.conf`, one directory under `system/apps/<package>/` holding an `app.toml` manifest, and one row in the registry that `forward_port.py` writes when the program starts.
The app owns its origin (`<name>-<suffix>.<workspace coordinate>`), its icon, its display name, and its instances.
The registered `name` is the developer-facing identifier: the supervisord program, the entry point, the origin label prefix, the share-grant key, and the key minds routes by.
It is not renameable (stable app ids and renaming are deferred, section 11); `display_name` is what users see and may change freely.

Every Python app, built-in or user-built, runs from its own uv tool environment, installed from the app's own `pyproject.toml` (which names its path dependencies itself).
The root venv belongs to the background services, agents, skills, and scripts, so an agent that breaks it cannot take down any app, and one app's dependency pins never constrain another's.
The build installs one tool per Python app directory, the update apply refreshes the tool environments whose app changed (and every app's when a shared backend manifest changed, section 8.3), and the `build-app` scaffold installs a tool for a user-built app.
An app's supervisord program line runs the tool's own entry point rather than `uv run`; services run under `uv run`.
The terminal and files apps are small Python launchers around ttyd and dufs, so they are tools too.
The manifest is what marks an app as running this way: a pre-manifest app has a `pyproject.toml` but no `app.toml`, runs `uv run <name>` from the root venv, and is left alone by the build and the apply.
Both forms are supported indefinitely, and every app, built-in or user-built, is a member of the root uv workspace's `system/apps/*` glob: one repo, one lockfile, one set of build and test commands.
Membership and isolation answer different questions: the lock keeps every environment on the same versions, and an app's own tool environment keeps it running while the root venv is rewritten or broken.
Converting a pre-manifest app to the manifest form is something the update-app skill may offer the next time the user edits that app, with the user present (section 11).

### 3.2 Instances

An app that declares `instances = true` in its manifest exposes the instances API (section 5.1) and is the sole authority on which instances exist, what each one's current URL is, what it is called, and what it is doing.
An app that declares `instances = false` has exactly one instance, the app root, addressed as `app:<name>`; opening it a second time focuses the existing tab.
An instance's URL may change over time as the user navigates, and the app records the current URL, so restoring an instance means opening the URL the app reports.
An instance key is one to 128 characters from letters, digits, dot, underscore, and hyphen, so it rides an address and a URL path unencoded.
Everything a tab shows is an instance: there are no page tabs outside the instance model, and an app that wants to show a page that is not yet backed by anything creates an instance for it first.

The shell never stores instances.
Its inventory is the union of every app's list, refreshed when an app nudges it and on a slow reconciliation sweep.
Layouts and project tab sets hold addresses only, and an address whose instance is no longer listed is dropped by observation.

Each instance also says how long it lives, in its `lifetime` field.
An `explicit` instance exists until something calls Delete; a closed chat or terminal keeps existing.
A `referenced` instance exists only while something references it: when the last reference goes, from every project's tab set and every client's layout, the shell calls the app's Delete.
The shell can decide this alone because it holds every reference on the server, and a failed Delete simply leaves the instance listed, so no second accounting is needed.
File-browser instances are `referenced`, so closing the last tab of one ends it the way closing a browser tab does; a chat's provisional new-chat page and its subagent views are `referenced` while the chat itself is `explicit`.

An instance's URL may carry one `{tab}` placeholder, which the shell replaces with an id it mints per tab.
An app that learns from its own backend that a tab now shows a different instance (the terminal, when a tmux client switches sessions or a session is renamed) tells the shell through one generic route, and the shell re-addresses that tab.

### 3.3 Views, layouts, and clients

A project's tab set is shared truth: adding an instance to a project is visible to every client at once.
A layout is client-scoped: each client arranges each view for itself, and decides which of the view's instances it docks.
Layouts are stored on the server, keyed by view and client, and the stored file is the truth: the browser writes it for the user's own gestures, the shell writes it for agent ops and its own bookkeeping, and every write is broadcast with the owning client id, which is what makes a later "follow another client" feature a rendering choice rather than new plumbing.
A client visiting a view for the first time starts from that view's seed layout for its device kind, then diverges.
Everything is a view like any other for arrangement purposes; its tab set is derived rather than stored.

### 3.4 Ownership

| Fact or verb | Owner |
|---|---|
| Which apps exist, their display name, icon, actions, criticality, priority | Manifest, mirrored into the registry |
| Whether an app is running; Stop and Start | Shell, via supervisord |
| Which instances exist, their URL, title, status, last-active | App |
| Create, Delete, Rename an instance | App |
| Where an instance's page currently is | App; the page reports it to the shell, which relays it to the app with the tab's key |
| Project names, colors, glyphs, tab sets, shortcuts | Shell, shared |
| Add to project, Remove from project | Shell, shared |
| Arrangement, docked set, last-focused per tab, active view | Shell, per client |
| Close (undock) a tab | Shell, per client |
| Workspace-level facts minds needs (service discovery events, owner-exec, share materials) | Minds, through its own contract with the workspace, which this model does not touch |

### 3.5 Invariants

- The shell imports nothing from `imbue.mngr` or the chat app, never runs the `mngr` binary, and names no app. Enforced by the shell's `test_project_ratchets.py`: an AST scan of every non-test module's imports, a regex ratchet on `mngr` argvs, and a regex ratchet on the literal `"chat"` in the package and its frontend.
- The shell reads and writes nothing under an mngr host or agent state directory. Its state lives under `data/.state/system_interface/`.
- The only app the shell needs in order to boot and render is itself. With no chat app registered, every view lands on the New Tab page and the launcher offers whatever is registered.
- Every app, built-in or user-built, is reachable by the shell only through the manifest, the registry, the instances API, and the browser-side contract.
- A tab's address is the whole of what the shell knows about what it shows.

## 4. The manifest and the registry

### 4.1 `app.toml`

Each app ships `system/apps/<package>/app.toml`:

```toml
name = "files"                 # required; the registered name, DNS-safe
display_name = "File Viewer"   # required; what users see
icon = "icon.svg"              # required unless internal = true; relative to the manifest
instances = true               # default false: a single implicit instance
critical = false               # default false; true routes updates through snapshot-and-rollback
priority = "user"              # default "user"; a built-in band name or "user" (see 8.2)
program = "files"              # default: name; the supervisord program that runs the app
internal = false               # default false; true hides the app from every open surface
instances_url = "http://localhost:8301"  # optional; where the shell reaches the instances API when it is not the app URL (5.1)

[default_shortcut]             # optional; the rail row a new project is seeded with (6.3)
action = "new"
mode = "focus"

[[actions]]
id = "new"                     # the id shortcuts and layout.py refer to
label = "New File Viewer"      # every action is a create: POST /_instances with the action id, then open the returned instance

[handles]                      # reserved for protocol and intent handlers (deferred, section 11); must be absent or empty
```

An app with `instances = false` declares no actions; the shell synthesizes its one action, `open`, which focuses the app's tab.
`forward_port.py --manifest <path>` reads every static field from the manifest, with `--name` and `--url` for the runtime facts and `--remove` for teardown; the script is stdlib-only so registration never depends on the root venv.
`--icon-file` and `--program` serve a pre-manifest app, which registers by `--name` without a manifest; `build-app` scaffolds a manifest and the manifest-driven registration line.
`--internal` and `--no-icon` serve registrations that have no app directory: owner-exec, the VM exec service, preview instances, and isolated test servers.

### 4.2 Registry rows

A registry row carries `name`, `url`, `label`, `icon`, `internal`, and `program` from the registration, and `display_name`, `instances`, `instances_url`, `actions`, `default_shortcut`, `critical`, and `priority`, all copied from the manifest at registration.
The `label` suffix has one job, an unguessable origin, and is never used as an identifier.
Liveness (`is_running`) is derived from supervisord and is never stored.
The app watcher, which writes the `service_registered` and `service_deregistered` events minds reads, reads `name`, `url`, `label`, and `icon` and ignores the manifest fields.

## 5. The app contract

### 5.1 The instances API (server-side)

An app with `instances = true` serves these routes, which only the shell calls, over loopback, at the registry's `instances_url` (the app URL unless the manifest says otherwise):

| Route | Meaning |
|---|---|
| `GET /_instances` | `{"instances": [{"key", "url", "title", "status", "lifetime", "last_active", "renameable"}]}`; `url` is a path under the app origin, optionally carrying `{tab}` |
| `POST /_instances` | Create one. Body `{"action": "<id>", "params": {}}`. Returns `{"key", "url"}`. |
| `DELETE /_instances/<key>` | Destroy the instance and whatever it owns. Idempotent. |
| `POST /_instances/<key>/rename` | Body `{"title"}`. 400 when the app does not support renaming. |
| `POST /_instances/<key>/location` | Body `{"path"}`. The shell relays a page's location report (5.2) here; the app records it as the instance's current URL. |

The API may live on a different port from the app's pages because a wrapped third-party server cannot serve it: the files app runs dufs at the app URL and a small sidecar at `instances_url`.
Nothing is proxied and no path is rewritten.
Browsers never call this API; the shell relays every instance verb (`POST /api/apps/<name>/instances`, and `.../instances/<key>/delete`, `rename`, `location`) so one code path serves the UI, `layout.py`, and the location relay.
An app that learns from its own backend which instance a tab now shows calls `POST <shell>/api/tabs/<tab-id>/instance {app, key}`, and the shell re-addresses that tab in the owning client's layout.

Status values are exactly `working`, `idle`, `attention`, `stopped`, and `error`.
There is no badge count and no detail line.

Whenever its list changes, an app nudges the shell: `POST <shell>/api/apps/<name>/changed` (loopback only, empty body), where `<shell>` resolves exactly as `system/scripts/layout.py` resolves it (`MINDS_WORKSPACE_SERVER_URL`, default `http://127.0.0.1:8000`).
The shell coalesces nudges per app over a short window, refetches that app's list once, and rebroadcasts only when the list differs from what it last sent, so a chat that flips status many times per turn or a terminal that renames repeatedly costs one refetch per burst.
The shell also sweeps every app's list on a slow interval as reconciliation, so a missed nudge costs latency, never correctness.

The shell exposes the merged inventory on its WebSocket as one message, `apps_updated`, carrying every app with its running state and its instances.
Apps with `instances = false` appear with one synthesized instance: key `""`, url `/`, title from `display_name`, status `idle` while running and `stopped` otherwise.

### 5.2 The browser-side contract

One postMessage module, `app_contract.js`, is served by the shell at `/_static/app_contract.js` with a permissive CORS header (it is a static ES module loaded from other origins) and imported by every app page that wants to speak it.
A ratchet in this repository forbids raw `postMessage` and `message` listeners outside that module and the shell's embed module.
Trust is the workspace origin family: the shell accepts messages only from frames whose origin is in the family, and apps accept messages only from `window.parent`.
Unknown types are ignored, shipped types never change meaning, and evolution is by adding types.

Shell to app:

| Type | Payload | Meaning |
|---|---|---|
| `shell:handshake` | `{clientId, deviceKind, viewId, address, tabId}` | Sent on every load of the frame. `address` is the tab's address, so a page that wants to know which instance it is can read it. |
| `shell:shown` / `shell:hidden` | `{}` | The tab became visible or stopped being visible in this client. Chat feeds its memory-shedding engine from this; the minimum terminal size across viewers (deferred, section 11) is what would read it next. |
| `shell:close-request` | `{}` | The close chord fired while this tab was active. |

App to shell:

| Type | Payload | Meaning |
|---|---|---|
| `shell:focused` | `{}` | The page received focus; the shell activates its tab. Cross-origin frames hide clicks from the shell, which is why this exists. |
| `shell:location` | `{path}` | The page reports where it is. The shell resolves the posting frame to its tab, and relays the path to the owning app's location route (5.1) with the tab's key. The page never needs to know its key. |
| `shell:open` | `{address}` | Dock an instance of this app beside this tab, focusing it if the client already shows it. The address must name this app. |

Every tab is an instance, so there is no bind step: a page that needs a tab before its backing thing exists has its app create an instance for it first (see the chat app's new-chat flow, 7.4).
The location report is the one message the shell forwards rather than consumes, and it is what lets a wrapped third-party page such as dufs take part with a one-line beacon and no knowledge of the instance model.
Single-instance apps are titled by their `display_name`; the shell stores no titles.

### 5.3 The embedder relay

The minds chrome embeds the workspace and accepts messages only from its direct child, the shell.
The shell's embed module therefore runs a dumb, bidirectional relay: any `minds:` message arriving from a child frame in the workspace origin family is forwarded up to the chrome unchanged, and any message arriving from the chrome is rebroadcast to every child frame.
The shell inspects no message types.
The minds chrome, the vendored embed contract, and the iframe security boundary know nothing of the relay: the chrome still sees exactly one child, the shell.

### 5.4 The instances library

`system/libs/app_instances/` is the shared implementation every multi-instance app uses, so that a user who wants two Jupyter notebooks or two dashboards side by side pays nothing new.
It provides:

- a Flask blueprint implementing section 5.1 over a pluggable instance source, with a JSON store under `data/.apps/<name>/instances.json` for apps whose instances have no other backing state;
- the nudge to the shell;
- a sidecar launcher for wrapped third-party servers: one supervisord program that serves the blueprint over the JSON store on a loopback port, registers the app with that port as its `instances_url`, and runs the wrapped server as its child at the app URL.

Each instance of a wrapped server is a record of where its page is, and the location relay (5.2) keeps that current for a page that carries the one-line beacon.
A wrapped server that carries no beacon still works, with every instance opening at the path its create gave it.

## 6. The shell

### 6.1 State

All shell state lives under `data/.state/system_interface/`:

- `projects.json`: `{version, last_active_view, projects: [{id, name, color, glyph, tabs: [address], shortcuts: [{app, action, mode}]}]}`.
- `layouts/<view-id>/<client-id>.json`: `{dockview, device_kind, updated_at}`, each panel's `params` in the dockview document naming what it shows (`kind`, `address`, `tabId`, `lastFocusedMs`).
- `layouts/<view-id>/seed.<device-kind>.json`: the seed a new client of that device kind starts from; rewritten from the most recently saved layout of that kind.
- `clients.json`: `{client_id: {device_kind, active_view, last_seen}}`; layouts of clients unseen for ninety days are pruned.
- `migrated.json`: the migration's marker (section 9).

Client activity attribution for `layout.py context` comes from `POST /api/client-activity`, which the chat app calls when a message is sent and the shell itself records when a view switches.

### 6.2 Views and clients

The active view is per client and stored on the server beside the client record, so a client resumes where it was.
The WebSocket carries `projects_updated` (shared) and `layout_updated {viewId, clientId, saveId}` (scoped); a client applies layout updates only for its own id, which is the hook a later follow mode attaches to.
The client id lives in the browser's local storage, so two windows of one browser on one workspace are one client and mirror each other's arrangement and active view by design; each window mints a save id per save and skips the broadcast of its own saves, which is the only per-window state there is.
Everything's tab set is the inventory; a project's tab set is its stored addresses.
A new project, and a new workspace's first landing, shows the New Tab page and nothing else.

### 6.3 Shortcuts and the New Tab page

A shortcut is `(app, action)` with a per-project mode, `focus` or `new`.
Focus goes to the most recently focused tab of that app in this client's layout and runs the action only when there is none; new always runs the action.
A new project's shortcut list is seeded from every registered app whose manifest declares a `default_shortcut`, in registry order, so the built-in rows (chat `new` in new mode; terminal, files, and browser `new` in focus mode) carry no shell code, and a user-built app pins itself to no project unless its manifest says so.
Everything's rail shows a fixed row for every registered app that declares an action.
The New Tab page offers every action of every registered app, then the view's instances, then everything else on the machine, ordered by app-reported last-active.

### 6.4 Verbs

The tab menu and the rail row build from one definition keyed by capabilities, not by kind:

- Refresh: reload the iframe.
- Share the app: through the minds chrome.
- Rename: shown when the instance reports `renameable`; calls the app.
- Add to project, Remove from project: shell, shared.
- Close: undock in this client. When this was the last reference to a `referenced` instance, the shell also calls the app's Delete.
- Delete: shown for `explicit` instances of `instances = true` apps; calls the app. The tab disappears when the app's list no longer carries the instance.
- Stop and Start the instance: shown when the instance reports `stoppable` (a chat's agent, a browser's Chromium, a terminal's session); calls the app, which keeps the instance and answers it as `stopped` until started again.
- Stop and Start the app: supervisord via the shell, for apps with a `program` that are not `critical`. On a single-instance app's tab, where the two coincide; for every other app, on the rail's per-app row menu (the app's presence in a view), never on an instance's tab.

A stopped app's tabs render a placeholder with a Start button; instances of a stopped app show `stopped`. A stopped instance of a running app keeps its page (a stopped chat's transcript stays readable; the browser's viewer shows a stopped overlay with a Start button).

### 6.5 `layout.py`

The agent-facing helper speaks addresses.
Every op targets exactly one client, the requester's by default or `--client <id>`, and is applied by the shell to that client's layout file, so no browser needs to be connected for it to land; `--view <name>` names the view whose layout the op edits and switches that client to it.
Only the verbs with nothing to store (maximize, restore, refresh, the interface reload) travel to the browser as messages.
`open app:<name>` runs the app's default action (its `default_shortcut` action, else the first one its manifest declares) through the relay and docks the fresh instance it made, whatever the client already shows; `open app:<name> --action <id>` runs a named action; `open app:<name>?instance=<key>` docks an existing instance.
`list` prints apps and instances with status from the inventory.
`rename`, `delete`, `stop`, and `start` take an instance address and call through to the app.
`replace-url <address> <path-or-url>` navigates an instance through the app's location route, the same fact a page reports for itself; the shell reloads a docked frame only when the instance's listed URL differs from what that frame last reported, so a page's own reports never reload it and an agent's navigation does.
`context` and `views` read the client records.
The spellings `chat:`, `terminal:`, `service:`, `url:`, `subagent:`, and `chat-terminal:` are refused with an error that names the address to use instead.

### 6.6 Recovery

The shell is the bare-origin entry and the recovery surface.
The not-built placeholder embeds the terminal app when one is registered and otherwise shows its prose alone.
Nothing in the shell depends on any other app being up.

### 6.7 The switcher index and deep links

The minds chrome is meant to carry a fast switcher that jumps to any workspace, view, app, action, or instance, and can attach a client to another client's layout.
The switcher is deferred (section 11), but the shell exposes everything it needs, as two requirements:

- `GET /api/inventory` returns one document: every project (id, name, color, glyph), the Everything view, every app (name, display name, icon, running state, actions) with its instances (address, title, status, last-active), and every known client (id, device kind, active view, last seen).
  It is the same data the WebSocket already carries, served once for a caller that holds no socket.
- A deep link the shell honors on load for the requesting client: `/?view=<view-id>` switches that client to the view, `&open=<address>` docks (or focuses) that instance in it, `&action=<app>:<action-id>` runs an action, and `&follow=<client-id>` attaches the client to another client's layout once follow mode exists.
  Unknown or stale targets fall back to the view's current state with no error page, since a switcher entry can outlive what it points at.

Every target the switcher can name is therefore an existing identity: a workspace id (minds), a view id, an address, an `(app, action)` pair, or a `(view, client)` pair.

## 7. The built-in apps

### 7.1 Terminal

The terminal is a small Python package, `system/apps/terminal`, that runs ttyd as its child and serves the instances API beside it.
Instances are tmux sessions named `terminal-<N>`; create allocates the lowest free number and makes the session at once, delete kills the session, rename changes the title alone (the key and the session name never change; the app matches a terminal to its session by tmux's session id and the session's creation time), and tmux hooks notify the terminal app of session switches and renames.
An instance's URL is `/?arg=_&arg=session&arg=<name>&arg={tab}[&arg=<workdir>]`, so the app maps each ttyd client's pty to the tab it serves; when a client switches sessions inside tmux, the app re-points that client's tab through the shell's tab route, and a rename changes no tab and only refreshes the list.
Status is `idle`, or `stopped` for a terminal whose session tmux lost and the app did not recreate (the user stopped it); a running foreground command is not distinguished (section 11). A session's shell runs in the `terminal-session` memory band, the user-service level.
The ttyd dispatch directory is `data/.state/terminal/commands/`; the terminal app installs `session.sh` and `workdir.sh` there, and `agent.sh`, the dispatch behind a chat's terminal back face, which it installs on the chat app's behalf.
Taking the minimum size across viewers of a session is deferred (section 11), and the `shell:shown` and `shell:hidden` signals are what it will read.

### 7.2 Browser

The browser daemon serves the `/_instances` routes as an adapter over its own browser list, create, and close, reports `working` while an agent holds control and `idle` otherwise, and nudges the shell on fleet changes.
An instance's URL is `/?session=<name>`.
A browser is `stoppable`: stop ends its Chromium after checkpointing its tabs and keeps the browser, its profile, and its tab list, reported as `stopped` (the viewer shows a stopped overlay with a Start button); start relaunches it on those tabs from the same profile; a stopped browser does not count toward the fleet cap and is restored as stopped after a daemon restart.
The daemon checkpoints every browser's tabs before Chromium is signalled (the browser program is stopped without its process group), so a restart never loses a tab list.

### 7.3 Files

The files app is `system/apps/files`, the instances library's sidecar launcher around dufs: dufs serves the app URL, and the sidecar serves the instances API from a JSON store at the app's `instances_url`.
An instance is a key and the path it was last at; its URL is that path under the dufs origin.
The dufs frontend carries a one-line location beacon (the `shell:location` message), the shell relays it to the sidecar with the tab's key, and the sidecar records it, so a file browser reopens at the folder it was showing.
The migration (section 9) imports the keys and paths of a pre-arc workspace's file browsers into the store.
Every file-browser instance is `referenced`, so it is deleted by the shell once no project and no client refers to it, and nothing lingers in Everything.

### 7.4 Chat

The chat app, `system/apps/chat/`, holds everything that concerns chats: the harness watchers, transcripts, sends, queue and interrupt handling, model choice, provider accounts and sign-in, uploads, the latchkey catalog proxy, memory-shedding retagging of chat agents, and its own frontend bundle.
It runs from its own uv tool environment (3.1), installed with the mngr harness plugins that `system/config/mngr_plugins.toml` assigns to `chat`; the shell's tool needs no mngr plugin.
Its instances are the workspace's chat agents, listed from `mngr observe`, excluding the primary services agent; keys are agent ids, URLs are `/<agent-id>`, titles are display names, rename goes through `mngr rename`, delete through `mngr destroy`, and status maps thinking and tool-running to `working`, a pending permission to `attention`, and a stopped agent to `stopped`.
Its `new` action creates a provisional instance: the chat backend mints the agent id before it runs `mngr create`, so the instance is keyed by that future id, and the tab never changes address.
Creation starts at once on the pinned default account (a star in the provider menu; `AccountIndex.default_account`), else the most recently used one, else the oldest; with nothing signed in the chat waits, and its page at `/<agent-id>` shows the account chooser, whose sign-in launches the chat under the same id.
While the create runs the page shows the composer over an empty transcript (a message typed then is held and delivered when the agent lands); a failed create shows the reason in the tab with a retry; the transcript takes over when the agent registers.
A provisional instance is `referenced`, so one whose tab is closed before the agent exists is deleted by the shell like any other unreferenced instance.
A subagent view is a chat instance too, keyed `<agent-id>.<session-id>`, `referenced`, created on demand by the `subagent` action when the user opens one from the parent chat's page, which then docks it with `shell:open`; agents and sub-agents are just chats.
Provider accounts live under `~/.minds/accounts`: chats bind to them by absolute paths in their env files and credential symlinks, and the store is chat-owned state whatever its path.
The chat app serves every `/api/agents/...` route at its own origin; the shell serves a plain `/api/health` for probes.
The first-chat claim and `/welcome` are the chat app's; the shell creates nothing.
The chat app's manifest declares `critical = true` and `priority = "chat"`, a band in `oom_priority.bands` sitting below the shell and above every chat agent.
Worker agents that chats spawn are listed as instances too; they are chats with a different origin label.

## 8. Sharing, memory, and updates

### 8.1 Sharing

The share gateway re-renders from the registry, so the chat origin is claimed like every app's.
A workspace-level grant admits the chat origin directly, and a per-app grant (the grants file keys apps as `[services.<name>]`, so `[services.chat]`) can narrow a visitor to it.
Read-only sharing of one chat is deferred (section 11).

### 8.2 Memory shedding

The backstop listener resolves a program's band from the registry: the row whose `program` is the program name gives its `priority`, and a program with no row falls back to the built-in tables by program name, then to the user band.
The chat app retags chat agents dynamically, fed by `shell:shown` and `shell:hidden` and by its own send path.

### 8.3 Updates

The update apply refreshes the tool environment of every manifest app whose directory changed (and of every manifest app when a shared backend manifest changed), holds every app whose manifest says `critical = true` and `instances = true` to its instances API after the restart (beside the shell's health route), and treats every app whose manifest says `critical = true` as a snapshot-and-rollback target alongside the shell.
A program the merge adds needs no `supervisorctl reread` step: the apply restarts the services agent, whose bootstrap window execs a fresh supervisord that reads the merged program table.
The apply also pre-flights the merged chat app beside the shell (`chat-app --preflight`, a boot that reconciles no accounts, runs no agent manager, and registers nothing), since the chat is the process that imports mngr and the harness plugins.
Full per-app generalization of the apply is deferred (section 11).

### 8.4 External callers

Anything outside the workspace that calls a shell or chat route is a wire contract: the minds_evals bridge in the mngr repo (create chat, list agents, send, read events) targets the chat origin, and the update apply's probes target the shell's health route.
[mngr_side_changes.md](mngr_side_changes.md) lists every such caller in the mngr repo; the two repositories are released together.

## 9. Migration

One script, `system/scripts/migrate_workspace_layouts.py`, runs once per workspace, from bootstrap at every boot (best-effort, behind its marker) and from the update apply before the restart (warning-only), guarded by a marker.
It is deterministic, and never destructive: the old files are untouched, an output that already exists is kept, and the two app stores only ever gain records.
The exact rules are in [phase_09_migration.md](phase_09_migration.md):

| Old | New |
|---|---|
| `chat:<agent-id>` | `app:chat?instance=<agent-id>` |
| `terminal:<name>` | `app:terminal?instance=<name>` |
| `service:browser?session=<name>` | `app:browser?instance=<name>` |
| `service:files?instance=<key>` | `app:files?instance=<key>`, with the key and its saved path imported into the files app's store |
| `service:<name>` (an app pin) | an `(app, open)` shortcut on that project for a single-instance app, the app's default action for one with instances |
| `service:<name>` panel of another app | `app:<name>` |
| `url:<hash>` panels | dropped; a URL opens as a browser instance |
| `subagent:<session-id>` panels | dropped; the chat app lists subagents as instances when their parent's transcript is read |
| the registry's last-active project id | dropped; the active view lives on each client's record, and a first-visiting client lands on the first project |
| `unpinned_shortcuts` and `shortcut_overrides` | the project's `shortcuts` list |
| `projects/<id>.json` and `<id>.mobile.json` | `layouts/<id>/seed.desktop.json` and `seed.mobile.json` |
| `member_last_used.json` | `last_focused_ms` on the matching seed-layout tabs |
| every terminal found | a record in the terminal app's store, so the terminal is listed before its tmux session exists again and its tabs survive the first observation |
| `member_titles.json` | terminal titles become the terminal record's title; the rest are dropped, since chats already carry theirs |
| `member_locations.json` | imported into the files app's store |

The old files are left in place and ignored (their deletion is deferred, section 11).

## 10. Phases

One pull request, built as ordered commits, each leaving the repository green.
Every phase below landed between 2026-09-02 and 2026-09-07; each phase file records what landed and where it departs from the sketch here (the terminal's revision after the phase 10 live test, phase 8's layout-file-as-truth, phase 9's dropped app rewrite, phase 11's dropped reread step).
Each phase names what must be exercised by hand in a dev workspace before the next starts.
Tests follow the code: the instances library and each app backend get unit tests, the shell's Playwright harness (`test_e2e.py`) learns to drive app iframes, the contract module and the mngr-free shell get ratchets, and the migration gets a fixture built from a real pre-arc workspace.

1. **Manifest, registry, and environments.** Add `app.toml` to every built-in, teach `forward_port.py` `--manifest`, extend the registry rows, switch the memory backstop to `priority`, and give every manifest app its own tool environment (3.1) in the build and the apply, with the manifest as the discriminator and apps still workspace members. Verify: every app registers with its display name and icon unchanged, and every built-in program runs from its own environment.
2. **Instances library.** Build `system/libs/app_instances` with the blueprint, the JSON store, the nudge, and the sidecar launcher, with unit tests against a stub app. Verify: a scratch app lists, creates, and deletes instances, and a wrapped scratch server does the same through the sidecar.
3. **Terminal app.** The Python package, tmux-backed instances, hook notifications, and the dispatch directory move. Verify: two terminals, rename, delete, reload survives.
4. **Files app.** The sidecar around dufs, per-instance paths in the store, and the beacon's rename to the contract message. Verify: two file browsers on different folders reopen where they were after a reload.
5. **Browser app.** The instances adapter and status. Verify: a fleet browser shows `working` while an agent drives it.
6. **Chat as a document.** The chat pages and their bundle become a separate document served by the system-interface process at a registered `chat` origin whose registry URL is the shell's own port (requests dispatched by Host label and by the `/_instances` path), with the instances API implemented over the existing agent manager, the browser-side contract module, and the embedder relay (permission cards live in chat pages, which are child frames from here on). Nothing moves between packages yet. Verify: every chat opens as an iframe at the chat origin and behaves as before; the shell's own bundle carries no chat views; a permission card reaches the minds inbox.
7. **Shell core.** Addresses, the app-agnostic inventory, the verb definition, the location relay, shortcuts as data, the New Tab page as the only empty state, the state files of 6.1, and deletion of the per-kind code and side stores; chat is already an ordinary iframe app, so the shell has no special case. Verify: every verb on every app from both the tab and the rail.
8. **Client-scoped layouts.** Client-tagged broadcasts with save ids and `layout_updated` first (the seam phase 7's review left open), then the layout file as the truth: the shell applies agent ops to the target client's file and broadcasts, replacing phase 7's browser-applied ops, their mutex, and the connected-browser requirement; then the per-client active view across a client's windows, the inventory endpoint and deep links (6.7), and the rest of `layout.py` (`--client`, `--action`, `--param`, the bare-URL open). The tab route, pruning, referenced-lifetime deletion, the address grammar, and the skill rewrites landed in phase 7. Verify: two browsers on one workspace arrange independently and share projects; two windows of one browser mirror each other; an agent op lands with no browser connected; closing the last file-browser tab everywhere removes the instance; a deep link lands on the named view and instance.
9. **Migration.** The layout migration script, its marker, and its wiring into bootstrap (best-effort at every boot) and the apply (warning-only, before the restart). The rewrite of pre-manifest apps and the removal of the `system/apps/*` member glob were dropped from the phase (3.1): both app forms stay supported and every app stays a workspace member. Verify: a workspace created before this arc upgrades with its projects, tabs, terminal titles, and folder paths intact.
10. **Chat app.** The move of the chat package and process to `system/apps/chat` with its own tool environment, program, manifest, and registry row; the chat page as its own frontend build beside the shell's, over the shared `system/libs/workspace_ui` library in one npm workspace; the sign-in flow moving into the chat (the shell's provider chooser, greeting, and launcher picker go; a chat waits for an account in its own tab); subagent instances; the first-chat claim; the shell's mngr-free invariant landing as an AST import ratchet plus a ratchet that the shell names no app. Verify: chats create, rename, delete, stop, and show status; permission cards reach the minds inbox through the relay; a fresh workspace lands on New Tab.
11. **Updates, sharing, and cleanup.** The apply changes, the external-caller retargeting (8.4), the sharing note in the share-gateway docs, the service-to-app rename across shell code and docs, deletion of the old stores, README and skill rewrites, and changelog entries.

After phase 11, an existing workspace is upgraded through update-self and exercised by hand as a real user, and the memory measurement from the simplify-chat-data-model work is repeated so the second process does not regress the workspace's memory story.

## 11. Deferred

- Stable app ids and app renaming; the home for an id, if ever needed, is the manifest.
- Protocol and intent handlers; only the reserved `handles` table exists.
- Follow mode between clients; the client-tagged layout broadcasts are its prerequisite.
- The fast switcher in the minds chrome; the inventory endpoint and deep links (6.7) are its prerequisites, and so is the chrome forwarding a deep link's query string to the shell frame, which no phase of this arc does.
- Minimum terminal size across viewers.
- A terminal status that distinguishes a running foreground command from an idle shell.
- Deleting the pre-arc layout files the migration leaves in place.
- Read-only sharing of one chat.
- Chat-internal cleanups from the old plan (per-chat channel consolidation, chooser refactoring, proto-agent broadcasts), which are invisible to the shell once chat is an app.
- Full per-app generalization of the update apply.
- Converting a pre-manifest user app to the manifest form (an offer the update-app skill can make with the user present, never an unattended update), and taking apps out of the root uv workspace, which one lockfile and one set of commands argue against.
- Pinning each app's tool environment to the root lock's versions (a constraints file exported from `uv.lock` at install time), so a tool environment cannot drift ahead of the lock by a patch version.
- Migrating the terminal, browser, and files instance sources onto a common implementation beyond the library.

## 12. Open questions

None.
The implementer-level details are settled in [contracts.md](contracts.md) and the per-phase specs beside this document.
Two decisions taken while writing them stand out because they preserve today's behaviour over the model's first draft: the terminal keeps its per-tab id and hook mechanism (7.1), and owner gating of verbs for shared-workspace visitors stays as it is today (deferred).
