# The workspace app model

This is the meta spec for the arc that turns the workspace into an operating system of web apps and moves chat out of the system interface into an app of its own.
It replaces the earlier `split-chat-apart` plan that lived in the mngr repo, which is deleted; that plan kept mngr and harness knowledge in the shell and froze the per-kind addressing schemes, both of which this spec reverses.
It is written for the people and agents implementing the arc, and it is the reference the implementation is judged against.
Implementation happens in this repository, in one pull request built as the ordered phases at the end of this document.

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

New code uses these names.
Existing code is renamed in the final cleanup phase.

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
| Shell | The system interface: window manager plus app management. Its registered app name stays `system_interface`. | chrome, desktop |
| Status | One of `working`, `idle`, `attention`, `stopped`, `error`, reported per instance by its app. | activity state, liveness |
| Manifest | `app.toml` beside an app's code: its static declarations. | |
| Registry | `data/.state/apps.toml`: the runtime record of registered apps, written by `forward_port.py`. | |

Terms retired outright: member, backgrounded, object, kind, chat-terminal, and the launcher-versus-rail distinction between "open new" and shortcuts.
"Backgrounded" is now just "an instance in the project's tab set that this client has not docked".

## 3. The model

### 3.1 Apps

An app is one `[program:*]` entry in `system/supervisord.conf`, one directory under `system/apps/<package>/` holding an `app.toml` manifest, and one row in the registry that `forward_port.py` writes when the program starts.
The app owns its origin (`<name>-<suffix>.<workspace coordinate>`), its icon, its display name, and its instances.
The registered `name` is the developer-facing identifier: the supervisord program, the entry point, the origin label prefix, the share-grant key, and the key minds routes by.
It is not renameable in this arc; `display_name` is what users see and may change freely.

### 3.2 Instances

An app that declares `instances = true` in its manifest exposes the instances API (section 5.1) and is the sole authority on which instances exist, what each one's current URL is, what it is called, and what it is doing.
An app that declares `instances = false` has exactly one instance, the app root, addressed as `app:<name>`; opening it a second time focuses the existing tab.
An instance's URL may change over time as the user navigates, and the app records the current URL, so restoring an instance means opening the URL the app reports.

The shell never stores instances.
Its inventory is the union of every app's list, refreshed when an app nudges it and on a slow reconciliation sweep.
Layouts and project tab sets hold addresses only, and an address whose instance is no longer listed is dropped by observation.

### 3.3 Views, layouts, and clients

A project's tab set is shared truth: adding an instance to a project is visible to every client at once.
A layout is client-scoped: each client arranges each view for itself, and decides which of the view's instances it docks.
Layouts are stored on the server, keyed by view and client, and layout changes are broadcast with the originating client id, which is what makes a later "follow another client" feature a rendering choice rather than new plumbing.
A client visiting a view for the first time starts from that view's seed layout for its device kind, then diverges.
Everything is a view like any other for arrangement purposes; its tab set is derived rather than stored.

### 3.4 Ownership

| Fact or verb | Owner |
|---|---|
| Which apps exist, their display name, icon, actions, criticality, priority | Manifest, mirrored into the registry |
| Whether an app is running; Stop and Start | Shell, via supervisord |
| Which instances exist, their URL, title, status, last-active | App |
| Create, Delete, Rename an instance | App |
| Project names, colors, glyphs, tab sets, shortcuts | Shell, shared |
| Add to project, Remove from project | Shell, shared |
| Arrangement, docked set, last-focused per tab, active view | Shell, per client |
| Close (undock) a tab | Shell, per client |
| Workspace-level facts minds needs (service discovery events, owner-exec, share materials) | Unchanged: the existing minds contract |

### 3.5 Invariants

- The shell imports nothing from `imbue.mngr` and never runs the `mngr` binary. Enforced by an import-linter contract and a ratchet.
- The shell reads and writes nothing under an mngr host or agent state directory. Its state lives under `data/.state/system_interface/`.
- The only app the shell needs in order to boot and render is itself. With no chat app registered, every view lands on the New Tab page and the launcher offers whatever is registered.
- Every app, built-in or user-built, is reachable by the shell only through the manifest, the registry, the instances API, and the browser-side contract.
- A tab's address is the whole of what the shell knows about what it shows.

### 3.6 What this replaces in the code

For orientation, the parts of `system/apps/system_interface` this model retires:

- The mngr coupling: `agent_discovery.py`, the `mngr observe` pipeline and chat concerns in `agent_manager.py`, `harnesses/`, `accounts.py`, `oom_prioritizer.py`, and the provider and filter arguments of `main.py`; all of it moves to the chat app.
- The per-kind stores: `member_titles.py`, `member_last_used.py`, `member_locations.py`, `app_instances.py`, `auto_open.py`, `client_activity.py`, and the layout files under the primary agent's `workspace_layout/`.
- The per-kind frontend code: the chat, terminal, browser, launcher, and app branches in `DockviewWorkspace.ts`, `objectMenu.ts`'s kinds, and the chat views and models in the shell bundle.
- The five address grammars in `projects.py`, `layout_ops.py`, and `system/scripts/layout.py`.

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

[[actions]]
id = "new"                     # the id shortcuts and layout.py refer to
label = "New File Viewer"
# Exactly one of:
create = true                  # POST /_instances, then open the returned URL
# path = "/new"                # open a pending tab at this path; the page binds later (5.2)

[handles]                      # reserved for protocol and intent handlers; must be absent or empty in this arc
```

`forward_port.py` gains `--manifest <path>` and reads every static field from it, keeping `--name` and `--url` for the runtime facts and `--remove` for teardown.
`--icon-file` and `--program` are removed in the cleanup phase once every app carries a manifest; `build-app` scaffolds a manifest and the manifest-driven registration line.
`--internal` and `--no-icon` stay for registrations that have no app directory: owner-exec, the VM exec service, preview instances, and isolated test servers.

### 4.2 Registry rows

The registry row keeps its current keys (`name`, `url`, `label`, `icon`, `internal`, `program`) and gains `display_name`, `instances`, `actions`, `critical`, and `priority`, all copied from the manifest at registration.
The `label` suffix keeps its one job, an unguessable origin, and is never used as an identifier.
Liveness (`is_running`) stays derived from supervisord and is never stored.
The minds side of the registry, the `service_registered` and `service_deregistered` events the app watcher writes, is unchanged.

## 5. The app contract

### 5.1 The instances API (server-side)

An app with `instances = true` serves these under its own origin, reachable from the shell over loopback at the registry URL:

| Route | Meaning |
|---|---|
| `GET /_instances` | `{"instances": [{"key", "url", "title", "status", "last_active", "renameable"}]}`; `url` is a path under the app origin |
| `POST /_instances` | Create one. Body `{"action": "<id>", "params": {}}`. Returns `{"key", "url"}`. |
| `DELETE /_instances/<key>` | Destroy the instance and whatever it owns. Idempotent. |
| `POST /_instances/<key>/rename` | Body `{"title"}`. 400 when the app does not support renaming. |
| `POST /_instances/<key>/location` | Body `{"path"}`. The page reports where it is; the app records it. |

Status values are exactly `working`, `idle`, `attention`, `stopped`, and `error`.
There is no badge count and no detail line.

Whenever its list changes, an app nudges the shell: `POST <shell>/api/apps/<name>/changed` (loopback only, empty body), where `<shell>` resolves exactly as `system/scripts/layout.py` resolves it (`MINDS_WORKSPACE_SERVER_URL`, default `http://127.0.0.1:8000`).
The shell answers by refetching that app's list and rebroadcasting.
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
| `shell:handshake` | `{clientId, deviceKind, viewId, address}` | Sent when the frame loads. `address` is the tab's address, so a page knows which instance it is. |
| `shell:shown` / `shell:hidden` | `{}` | The tab became visible or stopped being visible in this client. Chat feeds its memory-shedding engine from this; the terminal will feed its sizing from it. |
| `shell:close-request` | `{}` | The close chord fired while this tab was active. |

App to shell:

| Type | Payload | Meaning |
|---|---|---|
| `shell:focused` | `{}` | The page received focus; the shell activates its tab. Cross-origin frames hide clicks from the shell, which is why this exists. |
| `shell:bind` | `{key}` | A pending tab declares which instance it became. The shell rewrites the tab's address and adds it to the view's tab set. |
| `shell:open-tab` | `{url}` | Open a sibling page as a new tab beside this one. The URL must be on this app's own origin. |

A pending tab is one opened by a `path` action: its address is `app:<name>` and its layout record carries the action path, so a reload reopens the page; it is never in a project's tab set until it binds.
Location is not a browser-side verb: a page reports its path to its own app backend (5.1), never to the shell.
Single-instance apps are titled by their `display_name`; the shell stores no titles.

### 5.3 The embedder relay

The minds chrome embeds the workspace and accepts messages only from its direct child, the shell.
The shell's embed module therefore runs a dumb, bidirectional relay: any `minds:` message arriving from a child frame in the workspace origin family is forwarded up to the chrome unchanged, and any message arriving from the chrome is rebroadcast to every child frame.
The shell inspects no message types.
The minds chrome, the vendored embed contract, and the iframe security boundary are not changed.

### 5.4 The instances library

`system/libs/app_instances/` is the shared implementation every multi-instance app uses, so that a user who wants two Jupyter notebooks or two dashboards side by side pays nothing new.
It provides:

- a Flask blueprint implementing section 5.1 over a pluggable instance source, with a JSON store under `data/.apps/<name>/instances.json` for apps whose instances have no other backing state;
- the nudge to the shell;
- a proxying front for wrapped third-party servers: it serves each instance under `/i/<key>/`, proxies the rest of the path to the wrapped server, rewrites the wrapped server's path-prefix placeholder per instance where the server supports one, and records the location the patched frontend reports.

A wrapped server that cannot be patched still works as a single-instance app with no location restore, which is what most third-party tools want.

## 6. The shell

### 6.1 State

All shell state lives under `data/.state/system_interface/`:

- `projects.json`: `{version, last_active_view, projects: [{id, name, color, glyph, tabs: [address], shortcuts: [{app, action, mode}]}]}`.
- `layouts/<view-id>/<client-id>.json`: `{dockview, tabs: {panel_id: {address, path?, last_focused_ms}}, device_kind, updated_at}`; `path` is set only on a pending tab.
- `layouts/<view-id>/seed.<device-kind>.json`: the seed a new client of that device kind starts from; rewritten from the most recently saved layout of that kind.
- `clients.json`: `{client_id: {device_kind, last_seen}}`; layouts of clients unseen for ninety days are pruned.

Removed: the machine-wide title, last-used, and location stores, the app-instances allocator, the auto-open ledger, the client-activity log in the agent state dir, and everything under the primary agent's `workspace_layout/`.
Client activity attribution for `layout.py context` moves to `POST /api/client-activity`, which the chat app calls when a message is sent and the shell calls when a view switches.

### 6.2 Views and clients

The active view is per client and stored on the server beside the client record, so a client resumes where it was.
The WebSocket carries `projects_updated` (shared) and `layout_updated {viewId, clientId}` (scoped); a client applies layout updates only for its own id, which is the hook a later follow mode attaches to.
Everything's tab set is the inventory; a project's tab set is its stored addresses.
A new project, and a new workspace's first landing, shows the New Tab page and nothing else.

### 6.3 Shortcuts and the New Tab page

A shortcut is `(app, action)` with a per-project mode, `focus` or `new`.
Focus goes to the most recently focused tab of that app in this client's layout and runs the action only when there is none; new always runs the action.
The built-in rail rows are seeded from configuration as ordinary shortcuts (chat `new`, files `new`, browser `new`, terminal `new`) and carry no code of their own.
The New Tab page offers every action of every registered app, then the view's instances, then everything else on the machine, ordered by app-reported last-active.

### 6.4 Verbs

The tab menu and the rail row build from one definition keyed by capabilities, not by kind:

- Refresh: reload the iframe.
- Rename: shown when the instance reports `renameable`; calls the app.
- Add to project, Remove from project: shell, shared.
- Close: undock in this client.
- Delete: shown for instances of `instances = true` apps; calls the app. The tab disappears when the app's list no longer carries the instance.
- Stop and Start the app: shown for apps with a `program` that are not `critical`; supervisord via the shell.

A stopped app's tabs render the existing placeholder with a Start button; instances of a stopped app show `stopped`.

### 6.5 `layout.py`

The agent-facing helper keeps its subcommand surface and speaks addresses.
Every op targets one client, the requester by default or `--client <id>`; `--view <name>` switches that client to the view first, then applies the op to that client's layout of it.
`open app:<name>` runs the app's default action (the first one its manifest declares) in focus mode; `open app:<name> --action <id>` runs a named action; `open app:<name>?instance=<key>` docks an existing instance.
`list` prints apps and instances with status from the inventory.
`rename` and `delete` gain instance forms that call through to the app.
`context` and `views` are unchanged in spirit and read the client records.
The old spellings (`chat:`, `terminal:`, `service:`, `url:`, `chat-terminal:`) are removed, and every skill that used them is rewritten in the same change.

### 6.6 Recovery

The shell remains the bare-origin entry and the recovery surface.
The not-built placeholder embeds the terminal app when one is registered and otherwise shows its prose alone.
Nothing in the shell depends on any other app being up.

## 7. The built-in apps

### 7.1 Terminal

The terminal becomes a small Python package, `system/apps/terminal`, that owns ttyd.
Instances are tmux sessions named `terminal-<N>`; create allocates the lowest free number, delete kills the session, rename renames it, and the tmux hooks that today notify the shell notify the terminal app instead.
An instance's URL is `/?arg=session&arg=<name>`.
Status is `idle`; distinguishing a running foreground command is deferred.
The ttyd dispatch directory moves from the mngr agent state dir to `data/.state/terminal/commands/`, and any app may install a dispatch script there; the chat app installs `agent.sh` for its terminal back face.
Taking the minimum size across viewers of a session is deferred, and the `shell:shown` and `shell:hidden` signals are what it will read.

### 7.2 Browser

The browser daemon already lists, creates, and closes sessions; it gains the `/_instances` routes as an adapter over them, reports `working` while an agent holds control and `idle` otherwise, and nudges the shell on fleet changes.
An instance's URL is `/?session=<name>`.

### 7.3 Files

The files app becomes a thin server, `system/apps/files`, built on the instances library's proxying front over dufs, which keeps listening on a private port.
Each instance lives under `/i/<key>/`, the patched dufs frontend reports its path there, and the app records it, so a file browser reopens at the folder it was showing.
Keys and paths from the old layouts-derived instances are imported by the migration (section 9).
There is no garbage collection: a file browser instance nobody shows lingers in Everything until deleted, which is accepted over any second accounting of who still holds it.

### 7.4 Chat

The chat app moves wholesale to `system/apps/chat/`: the harness watchers, transcripts, sends, queue and interrupt handling, model choice, provider accounts and sign-in, uploads, the latchkey catalog proxy, memory-shedding retagging of chat agents, and its own frontend bundle.
It is an ordinary workspace member run as `uv run chat`, with the mngr harness plugins as its real dependencies rather than root dev dependencies; the `system-interface` tool no longer needs any mngr plugin.
Its instances are the workspace's chat agents, listed from `mngr observe`, excluding the primary services agent; keys are agent ids, URLs are `/<agent-id>`, titles are display names, rename goes through `mngr rename`, delete through `mngr destroy`, and status maps thinking and tool-running to `working`, a pending permission to `attention`, and a stopped agent to `stopped`.
Its `new` action is `path = "/new"`: the page holds the account chooser and the creation log and binds the tab once the agent exists.
Subagent views are chat pages opened with `shell:open-tab`.
Provider accounts move to `data/.apps/chat/accounts/`, and the one-time auth migration script moves with them.
The first-chat claim and `/welcome` are the chat app's; the shell creates nothing.
The chat app's manifest declares `critical = true` and `priority = "chat"`, a new band in `oom_priority.bands` sitting below the shell and above every chat agent.
Worker agents that chats spawn are listed as instances too; they are chats with a different origin label.

## 8. Sharing, memory, and updates

### 8.1 Sharing

The share gateway re-renders from the registry, so the chat origin is claimed automatically.
A workspace-level grant now admits the chat origin directly, and per-app grants (`[services.chat]` in the grants file, keeping that file's existing key name) can narrow it.
Read-only sharing of one chat becomes possible later and is not built here.

### 8.2 Memory shedding

`priority` in the manifest replaces the code table keyed by program name: the backstop listener reads the registry row for a program and falls back to the user band for anything without one.
The chat app keeps its dynamic retagging of chat agents, fed by `shell:shown` and `shell:hidden` and by its own send path.

### 8.3 Updates

The update apply learns two things: to `supervisorctl reread` and `update` after a merge so a newly added program starts, and to treat every app whose manifest says `critical = true` as a snapshot-and-rollback target alongside the shell.
Full per-app generalization of the apply is deferred.

## 9. Migration

One script, `system/scripts/migrate_workspace_layouts.py`, runs once per workspace, from bootstrap on the first boot after the update and from the update apply, guarded by a marker.
It is deterministic:

| Old | New |
|---|---|
| `chat:<agent-id>` | `app:chat?instance=<agent-id>` |
| `terminal:<name>` | `app:terminal?instance=<name>` |
| `service:browser?session=<name>` | `app:browser?instance=<name>` |
| `service:files?instance=<key>` | `app:files?instance=<key>`, with the key and its saved path imported into the files app's store |
| `service:<name>` (an app pin) | a `(app, new)` shortcut on that project |
| `service:<name>` panel of another app | `app:<name>` |
| `url:<hash>` panels | dropped; opening a URL now means a browser instance |
| `subagent:<session-id>` panels | dropped; they are reopened from the chat |
| the registry's last-active project id | the initial active view of every client |
| `unpinned_shortcuts` and `shortcut_overrides` | the project's `shortcuts` list |
| `projects/<id>.json` and `<id>.mobile.json` | `layouts/<id>/seed.desktop.json` and `seed.mobile.json` |
| `member_last_used.json` | `last_focused_ms` on the matching seed-layout tabs |
| `member_titles.json` | terminal titles become tmux renames; the rest are dropped, since chats already carry theirs |
| `member_locations.json` | imported into the files app's store |

The old files are left in place and ignored, and are deleted in a later release.

## 10. Phases

One pull request, built as ordered commits, each leaving the repository green.
Each phase names what must be exercised by hand in a dev workspace before the next starts.
Tests follow the code: the instances library and each app backend get unit tests, the shell's Playwright harness (`test_e2e.py`) learns to drive app iframes, the contract module and the mngr-free shell get ratchets, and the migration gets a fixture built from a real pre-arc workspace.

1. **Manifest and registry.** Add `app.toml` to every built-in, teach `forward_port.py` `--manifest`, extend the registry rows, and switch the memory backstop to `priority`. Verify: every app registers with its display name and icon unchanged.
2. **Instances library.** Build `system/libs/app_instances` with the blueprint, the JSON store, the nudge, and the proxying front, with unit tests against a stub app. Verify: a scratch app lists, creates, and deletes instances.
3. **Terminal app.** The Python package, tmux-backed instances, hook notifications, and the dispatch directory move. Verify: two terminals, rename, delete, reload survives.
4. **Files app.** The dufs front, per-instance paths, and the beacon patch. Verify: two file browsers on different folders reopen where they were after a reload.
5. **Browser app.** The instances adapter and status. Verify: a fleet browser shows `working` while an agent drives it.
6. **Shell core.** Addresses, the app-agnostic inventory, the verb definition, the browser-side contract module, the embedder relay, shortcuts as data, the New Tab page as the only empty state, and deletion of the per-kind code and side stores. Chat still renders in-process here, behind a transitional in-process instance source that speaks the same list shape and is deleted in phase 9, so the shell has no other special case. Verify: every verb on every kind from both the tab and the rail.
7. **Client-scoped layouts.** Server-side layouts by client, seeds, pruning, client-tagged broadcasts, per-client active view, `layout.py` and skill rewrites. Verify: two browsers on one workspace arrange independently and share projects.
8. **Migration.** The script, its marker, and its wiring into bootstrap and the apply. Verify: a workspace created before this arc upgrades with its projects, tabs, and folder paths intact.
9. **Chat app.** The move to `system/apps/chat`, the instances API over mngr, accounts, the first-chat claim, the manifest, the supervisord program, and the shell's mngr-free invariant landing as an import contract and a ratchet. Verify: chats create, rename, delete, stop, and show status; permission cards reach the minds inbox through the relay; a fresh workspace lands on New Tab.
10. **Updates, sharing, and cleanup.** The apply changes, the sharing note in the share-gateway docs, the service-to-app rename across shell code and docs, deletion of the old stores, README and skill rewrites, and changelog entries.

After phase 10, an existing workspace is upgraded through update-self and exercised by hand as a real user, and the memory measurement from the simplify-chat-data-model work is repeated so the second process does not regress the workspace's memory story.

## 11. Deferred

- Stable app ids and app renaming; the home for an id, if ever needed, is the manifest.
- Protocol and intent handlers; only the reserved `handles` table exists.
- Follow mode between clients; the client-tagged layout broadcasts are its prerequisite.
- Minimum terminal size across viewers.
- Read-only sharing of one chat.
- Chat-internal cleanups from the old plan (per-chat channel consolidation, chooser refactoring, proto-agent broadcasts), which are invisible to the shell once chat is an app.
- Full per-app generalization of the update apply.
- Migrating the terminal, browser, and files instance sources onto a common implementation beyond the library.

## 12. Open questions

- Whether the files front can rewrite dufs's path-prefix placeholder per instance without patching more of its frontend; if not, the front rewrites links in the proxied HTML for the one page that carries them.
- Whether the terminal app's hook-driven title updates need a debounce once they nudge the shell on every rename.
