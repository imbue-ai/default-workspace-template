# Workspace app model: contracts

This file holds every cross-cutting schema, route, message, and file format the phase specs beside it rely on.
Each phase file links here rather than restating a shape, so the implementer never reconciles two copies.
The vocabulary is the glossary in [plan-workspace-app-model.md](plan-workspace-app-model.md), and the phase files are `phase_01_*.md` through `phase_11_*.md` plus [mngr_side_changes.md](mngr_side_changes.md).

Every rule below is normative.
Where a phase changes a contract mid-arc (the shared-process phases 6 to 9), the phase file says so and this file describes the end state.

## 1. Identifiers and addresses

- An **app name** obeys `system/scripts/forward_port.py`'s existing rule: lowercase alphanumeric or underscore runs joined by single hyphens, at most 32 characters, not `localhost` or `auth`, not starting with `host-` or `agent-`.
- An **instance key** matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`.
  It is unique within its app and never changes for the life of the instance.
  Keys ride addresses, URLs, and JSON keys unencoded; a key never needs percent-encoding because its alphabet is URL-safe.
- An **address** is `app:<name>` for a single-instance app or `app:<name>?instance=<key>` for an instance.
  The parser splits at the first `?`, requires the remainder to be exactly `instance=<key>`, and rejects anything else.
  `app:<name>` for an app with `instances = true` is not an address of an instance; it names the app for `open` and `--action`.
- A **view id** is a project id (the slugified project name, as today) or the literal `everything`.
- A **client id** is the uuid the browser keeps in local storage under `si-client-id`, as today.
- A **tab id** is `tab-<16 hex>`, minted by the shell when a panel is created, kept in the client's layout record, and never reused.
- A **save id** is `save-<16 hex>`, minted by a window for each layout save it makes.

## 2. The manifest (`app.toml`)

Path: `system/apps/<package>/app.toml`, beside the app's code.
Parsed by the `app_manifest` library (section 14) with pydantic, `extra = "forbid"`.

| Field | Type | Required | Default | Rule |
|---|---|---|---|---|
| `name` | string | yes | | An app name (section 1). Must equal the `--name` passed at registration. |
| `display_name` | string | yes | | Non-empty, at most 64 characters. What users see. |
| `icon` | string | unless `internal` | | Path relative to the manifest, `.svg`, validated by `forward_port.py`'s existing `validate_icon` at registration. |
| `instances` | bool | no | `false` | `true` exposes the instances API. |
| `instances_url` | string | no | the app URL | `http://127.0.0.1:<port>` or `http://localhost:<port>`; where the shell reaches the instances API. Only allowed with `instances = true`. |
| `critical` | bool | no | `false` | No Stop verb; snapshot-and-rollback target in the apply. |
| `priority` | string | no | `"user"` | A key of `SERVICE_BANDS` in `oom_priority.bands` (which gains `chat`), or `user`. |
| `program` | string | no | `name` | The supervisord program that runs the app. |
| `internal` | bool | no | `false` | Hidden from every open surface. |
| `default_shortcut` | table | no | absent | `{action = "<id>", mode = "focus" \| "new"}`. `action` must be a declared action id, or `open` for a single-instance app. |
| `actions` | array of tables | no | `[]` | Each `{id, label, params?}`; `id` matches `^[a-z0-9][a-z0-9-]{0,31}$` and is unique; `label` non-empty. `params` is an optional array of `{name, label, required}` describing the create body's `params` keys, for documentation and `layout.py --param` validation only. Forbidden when `instances = false`. |
| `handles` | table | no | absent | Must be absent or empty in this arc. |

A single-instance app (`instances = false`) has exactly one synthesized action, `open`, labelled `Open <display_name>`, which the shell adds when it reads the registry; the manifest never declares it.

Built-in manifests:

| App | `instances` | `instances_url` | `critical` | `priority` | `default_shortcut` | `actions` |
|---|---|---|---|---|---|---|
| `system_interface` | false | | true | `system_interface` | none | none; also `internal = true` |
| `chat` | true | app URL | true | `chat` | `{action = "new", mode = "new"}` | `new` ("New Chat", params `account_id` optional), `subagent` ("Open subagent", params `parent` and `session` required, `description` optional: the subagent's title) |
| `terminal` | true | `http://127.0.0.1:7682` | true | `terminal` | `{action = "new", mode = "focus"}` | `new` ("New Terminal", params `workdir` optional) |
| `files` | true | `http://127.0.0.1:8301` | false | `files` | `{action = "new", mode = "focus"}` | `new` ("New File Viewer", params `path` optional) |
| `browser` | true | app URL | false | `browser` | `{action = "new", mode = "focus"}` | `new` ("New Browser", params `url` optional, from phase 8) |

The `terminal` and `files` manifests keep `icon` pointing at the existing `icon.svg` files (the terminal gains one, drawn in the house style; today it registers with `--no-icon`).
While the chat app is a second document of the shell's process (phases 6 to 9), its manifest also declares `program = "system_interface"`, `priority = "system_interface"` (the memory backstop resolves a program's band from the registry row that names it), and `internal = true` (the shell offers no row for it until phase 7 reads `critical` and lists apps from the inventory); each is a `# CLEANUP:` for phase 7 or 10.

## 3. The registry (`data/.state/apps.toml`)

Written only by `system/scripts/forward_port.py`, which becomes stdlib-only: `tomllib` to read and a private writer that emits the flat shape below.
The writer supports exactly the value types the registry uses: strings (emitted as basic strings with `\\`, `"`, and control characters escaped), booleans, and arrays of inline tables whose values are strings or booleans.

Each `[[apps]]` row:

| Key | Source | Notes |
|---|---|---|
| `name`, `url`, `label`, `icon`, `internal`, `program` | as today | `label` stays the unguessable origin label and is never an identifier. |
| `display_name` | manifest | Absent on manifest-less rows; the shell then uses `name`. |
| `instances` | manifest | Absent reads as `false`. |
| `instances_url` | manifest | Absent reads as `url`. |
| `critical` | manifest | Absent reads as `false`. |
| `priority` | manifest | Absent reads as `user`. |
| `default_shortcut` | manifest | Inline table `{action, mode}`. |
| `actions` | manifest | Array of inline tables `{id, label}`; `params` is not copied. |

`forward_port.py --manifest <path> --url <url>` reads the manifest with `tomllib`, validates `name` (must match the manifest), reads and validates the icon file, and upserts the row with every field above; `--name` may be given and must then equal the manifest's name.
`--name --url` without `--manifest` keeps today's behaviour for manifest-less registrations (`--internal`, `--no-icon`, `--program`, `--icon-file` unchanged until phase 11 removes `--icon-file` and `--program`).
`--remove` is unchanged.
The script validates only what it copies from files; the shell validates every row against the `AppManifest` model on read and logs and skips a row that fails, so a hand-edited registry degrades to a missing app rather than a crashed shell.

The app watcher and the minds side read the same keys they read today and ignore the new ones.

## 4. The instances API

Served by every app with `instances = true`, at the app's `instances_url`, over loopback, and called only by the shell.
JSON in and out; every error body is `{"detail": "<message>"}`.

### 4.1 The instance record

```json
{
  "key": "terminal-2",
  "url": "/?arg=_&arg=session&arg=terminal-2&arg={tab}",
  "title": "Terminal 2",
  "status": "idle",
  "lifetime": "explicit",
  "last_active": "2026-09-02T14:11:02.824Z",
  "renameable": true
}
```

- `url` is a path under the app's origin, starting with a single `/` (never `//`, which a browser would read as another host), at most 2048 characters, with no control characters.
  It may contain the literal `{tab}` once; the shell replaces it with the tab id of the tab that opens it.
  Every other character is emitted as the app wrote it.
- `status` is one of `working`, `idle`, `attention`, `stopped`, `error`.
- `lifetime` is `explicit` (exists until deleted) or `referenced` (the shell deletes it when no project tab set and no client layout references its address).
- `last_active` is an RFC 3339 UTC timestamp or `null`.
- `renameable` says whether `POST /_instances/<key>/rename` is accepted.
- `title` is non-blank after trimming surrounding whitespace and at most 256 characters; a rename body that breaks this is a bad title.

### 4.2 Routes

| Route | Request | Response | Errors |
|---|---|---|---|
| `GET /_instances` | | `200 {"instances": [record, ...]}` | `503 {"detail"}` while the app is initialising |
| `POST /_instances` | `{"action": "<id>", "params": {...}}` | `201 {"instance": record}` | `400` unknown action or bad params, `409` the app cannot create now (with a detail the shell shows verbatim), `503` initialising |
| `DELETE /_instances/<key>` | | `204` | none: an unknown key is `204` (idempotent) |
| `POST /_instances/<key>/rename` | `{"title": "<text>"}` | `200 {"instance": record}` | `400` not renameable or bad title, `404` unknown key, `409` title collision |
| `POST /_instances/<key>/location` | `{"path": "<path>"}` | `200 {"instance": record}` | `400` bad path or the app does not track location, `404` unknown key, `409` the app cannot navigate there now (the browser; see section 4.3) |

`path` obeys the same rule as `url`, minus the placeholder: rooted with a single slash, at most 2048 characters, no control characters; or, for an app that navigates to other sites' pages (the browser), an absolute `http` or `https` URL with a host, under the same length and character rules.
Each app takes the form that fits it and answers `400` for the other.
An app that accepts a location stores it as the instance's `url` (with the `{tab}` placeholder re-added if the app uses one) and nudges; an app that navigates to it keeps its instance `url` as it was and records the destination in its own state.
A `<key>` that fails the key rule of section 1 is `400` on every keyed route, `DELETE` included, before the app is consulted; the shell only ever sends keys it listed, so this names a caller bug rather than an absent instance.
A body that is not a JSON object, or not the route's shape, is `400`; the instances API reads bodies regardless of the request's content type.
Every mutating route, `DELETE` of an unknown key included, nudges the shell; the shell coalesces, so a spurious nudge costs one refetch at most.

### 4.3 Per-app behaviour

| App | Key | `url` | `title` | `status` | `lifetime` | `renameable` | Create | Delete | Location |
|---|---|---|---|---|---|---|---|---|---|
| terminal | tmux session name | `/?arg=_&arg=session&arg=<key>&arg={tab}[&arg=<workdir>]` (the leading `_` lands in `$0` of the `bash -c` dispatch snippet, as today's frontend sends it; `workdir` rides as the last argument when the create gave one) | the title the user gave it, else `Terminal <N>` for `terminal-<N>` and the name verbatim (see phase 3) | `idle`, or `stopped` when the session is absent from tmux and the store still holds the name | `explicit` | true | allocates the lowest free `terminal-<N>`, records it in the store; the session itself is created on first attach (`new-session -A`) as today; `params.workdir` optional | kills the session and drops the name | `400` |
| files | `files-<N>` | the stored path | `File Viewer <N>` | `idle` | `referenced` | false | allocates the lowest free number, stores `params.path` or `/` | drops the record | records the path |
| browser | browser name | `/?session=<key>` | `Browser <N>` or the legacy name | `working` while an agent holds control, else `idle` (a browser still launching included); `error` for a crashed browser | `explicit` | false | `POST /browsers`; `params.url` (optional, an absolute `http(s)` URL; from phase 8, `400` before it) is the first page the new browser opens on, so `layout.py open <url>` is one create rather than a create and a location the launching browser would refuse | `DELETE /browsers/<key>` | navigates the live browser's active tab to the absolute URL in `path` (a rooted path is `400` for this app) and checkpoints its fleet manifest; `409` while an agent holds the browser or while it is launching or crashed |
| chat | agent id, or `<agent-id>.<session-id>` for a subagent | `/<key>` | the agent's display name; `Subagent: <description>` for a subagent (the session id when the create gave no description); the minted display name for a provisional instance (`New chat` when there is none yet) | lifecycle stopped, done, or unknown `stopped`; else pending permission `attention`; else thinking or tool-running `working`; else `idle`; provisional `attention`; subagent `idle` | `explicit` for agents, `referenced` for provisional and subagent instances | true for agents, false otherwise | `new` mints the agent id and a provisional record (`409` with no signed-in account); `subagent` requires `parent` (a listed chat) and `session`, takes an optional `description`, and returns an existing record when one exists | `mngr destroy` for an agent; drops the record for a subagent; a provisional key is a no-op | `400` |

## 5. Shell routes apps and scripts call

All routes below are on the shell (`MINDS_WORKSPACE_SERVER_URL`, default `http://127.0.0.1:8000`).

| Route | Caller | Request | Response |
|---|---|---|---|
| `POST /api/apps/<name>/changed` | any app, loopback only | empty | `204`; unknown name `404` |
| `POST /api/tabs/<tab_id>/instance` | an app, loopback only | `{"app": "<name>", "key": "<key>"}` | `204`; unknown tab `404`; app mismatch with the tab's address `400` |
| `POST /api/client-activity` | the chat app on a send; the shell itself on a view switch | `{"client_id", "device_kind", "view_id", "kind": "message" \| "view_switch", "app"?, "key"?, "text"?}` | `204` |
| `GET /api/health` | probes | | `200 {"status": "ok", "is_frontend_built": bool}` |

Loopback-only routes reject non-loopback peers with `403`, as `/api/layout/broadcast` does today.

The shell coalesces `changed` nudges per app: the first nudge starts a 250 ms window, one refetch runs when it closes, and a broadcast follows only when the fetched list differs from the last broadcast list for that app.
The reconciliation sweep refetches every running app's list every 30 seconds.
An app whose fetch fails keeps its last known list with every instance's status rewritten to `error`; an app supervisord reports stopped keeps its last known list with status `stopped`.

## 6. Shell routes the browser calls

Unchanged routes: `GET /` and the SPA catch-all, `/assets/<path>`, `/plugins/<basename>`, `POST /api/layout/broadcast`, `POST /api/apps/<name>/stop`, `POST /api/apps/<name>/start`, `/api/ws`.

Instance verbs are relayed by the shell, so browsers never reach an `instances_url`:

| Route | Request | Response |
|---|---|---|
| `POST /api/apps/<name>/instances` | `{"action", "params"}` | the app's response, status and body passed through; `503 {"detail"}` when the app is unreachable |
| `POST /api/apps/<name>/instances/<key>/delete` | | passthrough |
| `POST /api/apps/<name>/instances/<key>/rename` | `{"title"}` | passthrough |
| `POST /api/apps/<name>/instances/<key>/location` | `{"path"}` | passthrough |

After any successful relay the shell refetches that app's list immediately rather than waiting for the nudge.

Projects and views:

| Route | Request | Response |
|---|---|---|
| `GET /api/projects` | | `{"projects": [project, ...]}` |
| `POST /api/projects` | `{"name", "color", "glyph"}` | `201 project` |
| `POST /api/projects/<id>/settings` | `{"name", "color", "glyph"}` | `200 project` |
| `POST /api/projects/<id>/delete` | | `200 {"fallback_view_id"}` |
| `POST /api/projects/<id>/tabs` | `{"address"}` | `200 project`; idempotent |
| `POST /api/projects/<id>/tabs/remove` | `{"address"}` | `200 project` |
| `POST /api/projects/<id>/shortcuts` | `{"app", "action", "mode"}` | `200 project`; replaces the entry for `(app, action)` |
| `POST /api/projects/<id>/shortcuts/remove` | `{"app", "action"}` | `200 project` |
| `GET /api/layouts/<view_id>?client=<client_id>` | | `200 layout` (the client's own, else the seed for its device kind, else `{"dockview": null, "tabs": {}}`) |
| `POST /api/layouts/<view_id>` | `layout` plus `client_id`, `save_id` | `204` |
| `GET /api/clients` | | `{"clients": [client, ...]}` |
| `GET /api/inventory` | | the inventory document (section 9) |

`project` is `{"id", "name", "color", "glyph", "tabs": [address], "shortcuts": [{"app", "action", "mode"}]}`.
`layout` is `{"dockview": <dockview JSON>, "tabs": {"<panel_id>": {"address", "tab_id", "last_focused_ms"}}, "device_kind", "updated_at"}`.
`client` is `{"id", "device_kind", "active_view", "last_seen"}`.

Everything (`view_id = everything`) accepts layout reads and writes and rejects every project route with `404`.

## 7. Shell state files

All under `data/.state/system_interface/`, written atomically (temp file plus rename) under one process-wide lock, exactly as the projects module does today.

- `projects.json`: `{"version": 1, "projects": [project, ...]}` in creation order.
- `layouts/<view_id>/<client_id>.json`: a `layout` (section 6).
- `layouts/<view_id>/seed.<device_kind>.json`: a `layout`; rewritten on every save by a client of that device kind.
- `clients.json`: `{"version": 1, "clients": {"<client_id>": {"device_kind", "active_view", "last_seen"}}}`.
- `migrated.json`: written by the migration (phase 9): `{"version": 1, "migrated_at", "source": "<old layout dir>"}`.

A client unseen for 90 days is dropped from `clients.json` together with every `layouts/*/<client_id>.json`, by a sweep that runs at shell start and daily.

## 8. The WebSocket

Route `/api/ws`, one connection per window.

Inbound (browser to shell):

| Type | Payload |
|---|---|
| `client_state` | `{"client_id", "device_kind", "active_view", "previous_view"}`; sent on connect and on every view switch; the shell records `active_view` and `last_seen` and logs a `view_switch` activity when `previous_view` differs |

Outbound (shell to browser):

| Type | Payload | When |
|---|---|---|
| `apps_updated` | `{"apps": [app, ...]}` | on connect, and whenever any app's row, liveness, or instance list changed (the whole inventory, diffed before sending) |
| `projects_updated` | `{"projects": [project, ...]}` | on connect and after any project write |
| `layout_updated` | `{"view_id", "client_id", "save_id"}` | after any layout save; a window applies it only when `client_id` is its own and `save_id` is not one it minted |
| `active_view_changed` | `{"client_id", "view_id"}` | after a `client_state` or a `load` op changed a client's active view; the other windows of that client switch |
| `tab_rebound` | `{"client_id", "view_id", "tab_id", "address"}` | after `POST /api/tabs/<tab_id>/instance`; the owning client re-addresses that tab, adds the address to the view's tab set through the projects route, and saves |
| `layout_op` | as today: `{"op", "args", "requester_agent_id", "target_client_id"}` | from `/api/layout/broadcast` |

`app` is `{"name", "display_name", "icon", "label", "url", "internal", "program", "critical", "actions": [{"id", "label"}], "default_shortcut", "is_running", "instances": [record, ...]}`.
A single-instance app carries one synthesized record: key `""`, url `/`, title `display_name`, status `idle` while running and `stopped` otherwise, lifetime `explicit`, renameable `false`.

Retired messages: `agents_updated`, `proto_agent_*`, `terminal_session`, `load_layout` (folded into `active_view_changed`), `project_saved`, `project_deleted`, `project_updated`, `project_members_changed`, `project_panel_removed`, `member_title_changed`, `member_last_used_changed`, `member_location_changed`.

## 9. The inventory document

`GET /api/inventory` returns:

```json
{
  "projects": [project, ...],
  "everything": {"id": "everything", "tabs": [address, ...]},
  "apps": [app, ...],
  "clients": [client, ...]
}
```

`everything.tabs` is every address of every listed instance, apps in registry order, instances in list order.
Each `client` entry here additionally carries `docked`, the addresses in that client's layout of its active view, so `layout.py list` can say where an instance is docked without reading layouts.

## 10. The browser-side contract (`app_contract.js`)

Served by the shell at `/_static/app_contract.js` with `Access-Control-Allow-Origin: *`, as an ES module.
Source: `system/apps/system_interface/frontend/src/app_contract.ts`, built as a separate library entry so the served file has no other imports.
Exports: `connectToShell({onHandshake, onShown, onHidden, onCloseRequest})` returning `{focused(), location(path), open(address)}`.

Trust: the shell accepts a message only when `event.source` is the `contentWindow` of an iframe it created and `event.origin` is in the workspace origin family (the same regex the minds chrome uses); the module accepts a message only when `event.source === window.parent`.
Unknown types are ignored; shipped types never change meaning.

| Direction | Type | Payload |
|---|---|---|
| shell to app | `shell:handshake` | `{"clientId", "deviceKind", "viewId", "address", "tabId"}`; sent after every `load` event of the frame, and again whenever the tab or view showing the page changes (a page outlives the pane that showed it) |
| shell to app | `shell:shown` / `shell:hidden` | `{}` |
| shell to app | `shell:close-request` | `{}` |
| app to shell | `shell:focused` | `{}` |
| app to shell | `shell:location` | `{"path"}`; the shell resolves the frame to its tab, remembers the path as that tab's last reported path, and relays it to the owning app's location route |
| app to shell | `shell:open` | `{"address", "title"?}`; the address must name the posting frame's app; the shell docks the instance beside the posting tab, or focuses the tab already showing it in this client. `title` is a display hint for the new tab, used until phase 7 titles tabs from the inventory (a `# CLEANUP:` on both sides) |

A frame's `load` event also clears the tab's last reported path.
The shell reloads a docked tab's frame when the instance's listed `url` differs from the tab's last reported path (with `{tab}` substituted), which is what makes an agent's `replace-url` land and a page's own reports inert.

The dufs frontend keeps its inline beacon, now posting `{"type": "shell:location", "path": ...}` to `window.parent`; the ratchet allowlist names the vendored dufs asset.
The vendored ttyd client keeps its focus listener (`ttyd-focus`, outbound from the shell), unchanged.

## 11. The embedder relay

In the shell's embed module: a `message` listener that forwards any message whose `type` starts with `minds:` from a child frame in the workspace origin family to `window.parent` unchanged, and forwards any message from `window.parent` to every child frame the shell created, unchanged.
The shell keeps its own handling of `minds:close-active-tab` and forwards it as well.
The shell inspects no payloads.

## 12. `layout.py`

Subcommands: `list`, `inspect`, `where`, `context`, `views`, `load`, `open`, `focus`, `split`, `close`, `move`, `rename`, `delete`, `maximize`, `restore`, `replace-url`, `refresh`, `shortcuts`, `shortcut set`, `shortcut remove`.

- Every op takes `--client <id>` (default: the client that most recently messaged the requesting agent, per `context`; else every connected client) and `--view <name>` (switch that client first).
- `open <address> [--action <id>] [--param name=value]...`: `app:<name>` runs `--action` or the app's `default_shortcut.action` or its first declared action, in focus mode; `app:<name>?instance=<key>` docks an existing instance; a bare `https://` URL means `open app:browser --action new --param url=<url>`; a bare word that is an app name means `app:<word>`.
- `rename <address> <title>` and `delete <address>` call the shell's relay routes.
- `replace-url <address> <path-or-url>` calls the relay's location route.
- `list` prints, per app: `name`, `display_name`, `is_running`, `actions`, and `instances` with `key`, `address`, `title`, `status`, `docked_in` (client ids).
- `views` prints every view with `tabs` (addresses) and `clients` (ids with device kind); `context` prints every client with `active_view`, `device_kind`, `connected`, and recent activity.
- Exit codes stay `0`, `1`, `3`.
- The old spellings (`chat:`, `terminal:`, `service:`, `url:`, `subagent:`, `chat-terminal:`) are errors that name the new form.

## 13. Deep links

Honoured by the shell on page load for the requesting client, then stripped from the URL:

- `?view=<view_id>`: switch to the view.
- `&open=<address>`: dock or focus the instance in that view.
- `&action=<app>:<action_id>`: run the action.
- `&follow=<client_id>`: reserved; ignored in this arc.

Unknown or stale targets are ignored silently.

## 14. Tool environments

- The manifest is the discriminator: every directory under `system/apps/` with both a `pyproject.toml` and an `app.toml` is a Python app that runs from its own uv tool: `uv tool install -e system/apps/<package> [--with-editable <plugin path>]...`, with the plugin list from `system/config/mngr_plugins.toml` where the app's manifest `name` appears in a plugin's `tools`.
- A directory with a `pyproject.toml` and no manifest is a pre-manifest app (scaffolded before this arc); it keeps running `uv run <name>` from the root venv, untouched by the build and the apply, until the migration (phase 9) rewrites it to the manifest form. There are exactly these two forms and the migration retires the second, so no code path ever handles a third.
- The tool's entry point is named after the program and is what the supervisord line runs.
- `system/scripts/build_workspace.sh` loops over the manifest directories.
- The update apply reinstalls the tool of every manifest app whose directory changed in the merge (excluding paths under `frontend/` and `static/`), and of every manifest app when a shared backend manifest changed, and snapshots the tool directory of every `critical` app before it does.
- Until phase 9, `system/apps/*` stays in the root workspace's member glob, so one lockfile covers the tree and a pre-manifest app's `{ workspace = true }` source in the root pyproject keeps resolving; `uv sync --all-packages` therefore also installs the manifest apps into the root venv, unused. Phase 9 takes apps out of the workspace once no pre-manifest app remains.
- The `app_manifest` and `app_instances` libraries are workspace members that apps depend on by path, so a tool install pulls them in editable.
- Services stay in the root venv and keep `uv run <name>`.

## 15. Memory priority

`oom_priority.bands.SERVICE_BANDS` gains `"chat": 25`.
The backstop listener resolves a program's band by finding the registry row whose `program` equals the program name and reading its `priority`; a program with no row, or a row with `priority = "user"`, gets `USER_SERVICE`, and the existing `_NON_SERVICE_PROGRAM_BANDS` table keeps covering the programs that are not apps.
The `oom_tag_service.py <key>` prefix keeps working unchanged for every program line.

## 16. Migration table

See [phase_09_migration.md](phase_09_migration.md); the mapping is the table in section 9 of the meta spec, made exact there.

## 17. Where app data and machine state live

Everything a program persists goes under `data/` (gitignored, restic-backed), in one of two places, chosen by what the record is about rather than by which program writes it:

- `data/.apps/<name>/`: everything an app persists about the user's things.
  Every app's instance records live here, at `data/.apps/<name>/instances.json`, whatever document shape the app uses (the library's `JsonStoreInstanceSource` for the files app; the terminal's own `{name, title, workdir}` records).
  A terminal's title and starting directory, and a file viewer's folder, are things the user chose, so they belong here even when the instance is backed by state elsewhere.
  The update-app skill treats this directory as the user's real data: verification never writes to it.
- `data/.state/` (a program's own under `data/.state/<name>/`): what a program keeps about this machine and can rebuild, or must not outlive it: the registry (`data/.state/apps.toml`), the terminal's dispatch scripts and pty-to-tab files (`data/.state/terminal/commands/`), and the shell's client layouts and client records (section 7).

A path an app takes on its command line (`--store`, `--state-dir`) defaults to these locations and is overridden only by tests.
