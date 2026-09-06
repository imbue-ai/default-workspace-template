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
| `chat` | true | app URL | true | `chat` | `{action = "new", mode = "new"}` | `new` ("New Chat", params `account_id` optional: a signed-in account to launch on; absent, the most recently used one, or a chat that waits for one when nothing is signed in), `subagent` ("Open subagent", params `parent` and `session` required, `description` optional: the subagent's title) |
| `terminal` | true | `http://127.0.0.1:7682` | true | `terminal` | `{action = "new", mode = "focus"}` | `new` ("New Terminal", params `workdir` optional) |
| `files` | true | `http://127.0.0.1:8301` | false | `files` | `{action = "new", mode = "focus"}` | `new` ("New File Viewer", params `path` optional) |
| `browser` | true | app URL | false | `browser` | `{action = "new", mode = "focus"}` | `new` ("New Browser", params `url` optional, from phase 8) |

The `terminal` and `files` manifests keep `icon` pointing at the existing `icon.svg` files (the terminal gains one, drawn in the house style; today it registers with `--no-icon`).
While the chat app was a second document of the shell's process (phases 6 to 9), its manifest also declared `program = "system_interface"` and `priority = "system_interface"`; phase 10 dropped both (the program is `chat`, the band is `chat`). (Phase 6 also declared `internal = true`, which phase 7 dropped when the shell started listing apps from the inventory.)

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
`--name --url` without `--manifest` keeps today's behaviour for manifest-less registrations (`--internal`, `--no-icon`, `--program`, `--icon-file` unchanged; a pre-manifest app registers this way for as long as it exists, so phase 11 keeps every flag).
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
| terminal | tmux session name | `/?arg=_&arg=session&arg=<key>&arg={tab}[&arg=<workdir>]` (the leading `_` lands in `$0` of the `bash -c` dispatch snippet, as today's frontend sends it; `workdir` always rides as the last argument: the one the create gave, else the directory the app runs from; only records written before that default lack it) | the title the user gave it, else `Terminal <N>` for `terminal-<N>` and the name verbatim (see phase 3) | `idle`, or `stopped` when the session is absent from tmux and the store still holds the name | `explicit` | true | allocates the lowest free `terminal-<N>`, records it in the store; the session itself is created on first attach (`new-session -A`) as today; `params.workdir` optional | kills the session and drops the name | `400` |
| files | `files-<N>` | the stored path | `File Viewer <N>` | `idle` | `referenced` | false | allocates the lowest free number, stores `params.path` or `/` | drops the record | records the path |
| browser | browser name | `/?session=<key>` | `Browser <N>` or the legacy name | `working` while an agent holds control, else `idle` (a browser still launching included); `error` for a crashed browser | `explicit` | false | `POST /browsers`; `params.url` (optional, an absolute `http(s)` URL; from phase 8, `400` before it) is the first page the new browser opens on, so `layout.py open <url>` is one create rather than a create and a location the launching browser would refuse | `DELETE /browsers/<key>` | navigates the live browser's active tab to the absolute URL in `path` (a rooted path is `400` for this app) and checkpoints its fleet manifest; `409` while an agent holds the browser or while it is launching or crashed |
| chat | agent id, or `<agent-id>.<session-id>` for a subagent | `/<key>` | the agent's display name; `Subagent: <description>` for a subagent (the session id when the create gave no description); the minted display name for a provisional instance (`New chat` when there is none yet) | a dead lifecycle (stopped or done; unknown is not evidence of death and counts as alive) `stopped`; else pending permission `attention`; else thinking or tool-running `working`; else `idle`; a provisional chat by its phase: `attention` while it waits for an account, `working` while its create runs, `error` when the create failed; subagent `idle` | `explicit` for agents, `referenced` for provisional and subagent instances | true for agents, false otherwise | `new` mints the agent id and a provisional record: with `account_id`, or with any signed-in account (the most recently used), the create starts at once; with nothing signed in the chat waits for an account and its page shows the provider chooser, whose sign-in launches it under the same id (`POST /api/agents/create-chat` with `agent_id`); a failed create keeps the record in the `failed` phase with the reason, and the page can try again on the same account; `subagent` requires `parent` (a listed chat) and `session`, takes an optional `description`, and returns an existing record when one exists | `mngr destroy` for an agent; drops the record for a subagent; drops a provisional chat that is waiting for an account or failed, and is a no-op for a create in flight | `400` |

## 5. Shell routes apps and scripts call

All routes below are on the shell (`MINDS_WORKSPACE_SERVER_URL`, default `http://127.0.0.1:8000`).

| Route | Caller | Request | Response |
|---|---|---|---|
| `POST /api/apps/<name>/changed` | any app, loopback only | empty | `204`; unknown name `404` |
| `POST /api/tabs/<tab_id>/instance` | an app, loopback only | `{"app": "<name>", "key": "<key>"}` | `204`; unknown tab `404`; app mismatch with the tab's address `400` |
| `POST /api/client-activity` | the chat app on a send; the shell itself on a view switch | `{"client_id", "device_kind", "view_id", "kind": "message" \| "view_switch", "app"?, "key"?, "text"?, "from_view_id"?}` | `204` |
| `GET /api/health` | probes | | `200 {"status": "ok", "is_frontend_built": bool}` |

Loopback-only routes reject non-loopback peers with `403`, as `/api/layout/broadcast` does today.

The shell coalesces `changed` nudges per app: the first nudge starts a 250 ms window, one refetch runs when it closes, and a broadcast follows only when the fetched list differs from the last broadcast list for that app.
The reconciliation sweep refetches every running app's list every 30 seconds.
An app whose fetch fails keeps its last known list with every instance's status rewritten to `error`; an app supervisord reports stopped keeps its last known list with status `stopped`.

## 6. Shell routes the browser calls

Unchanged routes: `GET /` and the SPA catch-all, `/assets/<path>`, `/plugins/<basename>`, `POST /api/apps/<name>/stop`, `POST /api/apps/<name>/start`, `/api/ws`.
`POST /api/layout/broadcast` keeps its path and is the agent-facing op route of section 12 (loopback only).

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
| `GET /api/layouts/<view_id>?client=<client_id>&device=<device_kind>` | | `200 layout` (the client's own, else the seed for its device kind, else `{"dockview": null, "tabs": {}}`); `device` names the seed for a client the shell has no record of yet |
| `POST /api/layouts/<view_id>` | `layout` plus `client_id`, `save_id`, `base_updated_at` | `200 {"updated_at"}`, the stamp written (the window's next `base_updated_at`), `null` when the body equalled the stored arrangement and nothing was written or broadcast; `409 {"detail"}` when the stored layout's `updated_at` is newer than `base_updated_at` (the window refetches and applies the stored one) |
| `GET /api/clients` | | `{"clients": [client, ...]}`; a window reads its own record here on boot to learn its active view |
| `GET /api/inventory` | | the inventory document (section 9) |

`project` is `{"id", "name", "color", "glyph", "tabs": [address], "shortcuts": [{"app", "action", "mode"}]}`.
`layout` is `{"dockview": <dockview JSON>, "tabs": {"<panel_id>": {"address", "tab_id", "last_focused_ms"}}, "device_kind", "updated_at"}`.
`client` is `{"id", "device_kind", "active_view", "last_seen", "is_connected"}`; `is_connected` says whether any window of the client holds the WebSocket right now.
`base_updated_at` is the `updated_at` of the layout the window last fetched or last saved successfully, `null` for a view it has only ever seen empty.

Everything (`view_id = everything`) accepts layout reads and writes and rejects every project route with `404`.

## 7. Shell state files

All under `data/.state/system_interface/`, written atomically (temp file plus rename) under one process-wide lock, exactly as the projects module does today.

- `projects.json`: `{"version": 1, "projects": [project, ...]}` in creation order.
- `layouts/<view_id>/<client_id>.json`: a `layout` (section 6).
- `layouts/<view_id>/seed.<device_kind>.json`: a `layout`; rewritten on every save a browser of that device kind makes.
  The shell's own writes (an agent op, a tab rebind, a pruned address) never copy a client's layout over a seed: a prune or a rebind edits the seed files directly, and an agent op edits only the target client's file.
  An op on a view the client has no file for first materializes the client's copy from the seed of its device kind.
- The client layout file is the truth of the arrangement: the browser writes it through the save route for the user's own gestures, and the shell writes it for agent ops and its own bookkeeping, and every write is followed by a `layout_updated` broadcast (section 8).
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
| `layout_updated` | `{"view_id", "client_id", "save_id"}` | after any write of a client layout (a browser's save, an agent op, a tab rebind, a prune); the shell mints the save id of its own writes; a window applies it only when `client_id` is its own, the view is the one it shows, and `save_id` is not one it minted, and then only when the fetched `updated_at` differs from the one it holds |
| `active_view_changed` | `{"client_id", "view_id"}` | after a `client_state` report or a `load` op (or an op's `--view`) changed the client's stored active view; never when the report names the view already stored; the other windows of that client switch and report back without a previous view |
| `tab_rebound` | `{"client_id", "view_id", "tab_id", "address"}` | after `POST /api/tabs/<tab_id>/instance`; the owning client re-addresses that tab, adds the address to the view's tab set through the projects route, and saves |
| `layout_op` | `{"op", "args", "requester", "target_client_id"}` | only the four transient verbs of section 12 (`maximize`, `restore`, `refresh`, `reload_system_interface`); `requester` is the address of the instance that posted the op (its own chat), `""` when unknown, and is what `self` resolves to; `target_client_id` names the client whose windows apply it, `null` for the two machine-wide forms |

`app` is `{"name", "display_name", "icon", "label", "url", "internal", "program", "critical", "instances_url", "has_instances", "actions": [{"id", "label"}], "default_shortcut", "is_running", "is_listed", "instances": [record, ...]}`.
`is_listed` is false until the app's instances API has answered a list once (a single-instance app's synthesized record counts): a client prunes a tab whose address is missing only from a list that has arrived, never from the empty seed.
A single-instance app carries one synthesized record: key `""`, url `/`, title `display_name`, status `idle` while running and `stopped` otherwise, lifetime `explicit`, renameable `false`.

Retired messages: `agents_updated`, `proto_agent_*`, `terminal_session`, `load_layout` (folded into `active_view_changed`), `project_saved`, `project_deleted`, `project_updated`, `project_members_changed`, `project_panel_removed`, `member_title_changed`, `member_last_used_changed`, `member_location_changed`.

`agents_updated` and `proto_agent_*` outlived phase 7 as a marked carve-out until phase 10 gave the chat app a socket of its own: the chat pages read `/api/ws` on the chat's origin, which sends `agents_updated`, `proto_agent_created` (a provisional chat's whole record, sent again when its phase changes) and `proto_agent_completed` (`{"agent_id", "success", "error"}`; `success` false with a reason is a failed create, false with `null` a chat discarded before it launched). Phase 8 replaced `load_layout` with `active_view_changed`, and moved `open`, `focus`, `split`, `close`, and `move` off the socket altogether: the shell applies them to the layout file and the file's `layout_updated` is what the windows see.

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
`clients` is every stored client record (section 7's retention), each carrying `is_connected` and additionally `docked`, the addresses in that client's layout of its active view, so `layout.py list` can say where an instance is docked without reading layouts.

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
| app to shell | `shell:open` | `{"address"}`; the address must name the posting frame's app; the shell docks the instance beside the posting tab, or focuses the tab already showing it in this client, and titles the tab from the inventory (phase 6 carried a `title` hint, which phase 7 dropped) |

The shell clears the tab's last reported path when it points the frame at a url itself; a page's own navigation, and the report it posts while loading (before the frame's `load` event), leave it standing.
The shell reloads a docked tab's frame when the instance's listed `url` differs from the tab's last reported path (with `{tab}` substituted), which is what makes an agent's `replace-url` land and a page's own reports inert.

The dufs frontend keeps its inline beacon, now posting `{"type": "shell:location", "path": ...}` to `window.parent`; the ratchet allowlist names the vendored dufs asset.
The vendored ttyd client keeps its focus listener (`ttyd-focus`, outbound from the shell), unchanged.

## 11. The embedder relay

In the shell's embed module: a `message` listener that forwards any message whose `type` starts with `minds:` from a child frame in the workspace origin family to `window.parent` unchanged, and forwards any message from `window.parent` to every child frame the shell created, unchanged.
The shell keeps its own handling of `minds:close-active-tab` and forwards it as well.
The shell inspects no payloads.

## 12. `layout.py` and the op route

Subcommands: `list`, `inspect`, `where`, `context`, `views`, `load`, `open`, `focus`, `split`, `close`, `move`, `rename`, `delete`, `maximize`, `restore`, `replace-url`, `refresh`, `shortcuts`, `shortcut set`, `shortcut remove`.

The script posts `{op, args, requester}` to `POST /api/layout/broadcast` on the shell (loopback only; the path is historical): `requester` is the caller's own chat as an address (`app:chat?instance=$MNGR_AGENT_ID`), which is what `self` names and how the shell attributes the op to a client; the shell itself names no app.
The client's layout file is the truth of the arrangement, so the shell applies every arrangement op to that file itself and no browser needs to be connected for an op to land.

- **The target client.** Every op that reads or changes one client's arrangement resolves to exactly one client: `--client <id>`, else the client that most recently messaged the requester's instance (the client-activity log), else the one connected client. When none of those settles it, the op fails with `412` and a detail that lists the connected clients and their views and asks for `--client`; the shell never guesses across clients and never applies an op to every client. A `--client` with no record is `404`. `context`, `views`, `list`, and the relay verbs `rename`, `delete`, and `replace-url` reach the whole machine and take no `--client`; `refresh <app>` and the interface reload are machine-wide too.
- **The target view.** `--view <name>` (a project's name or id, or Everything; `--layout` stays as an alias) names the view whose arrangement the op edits; without it the op edits the client's active view. A `--view` that differs from the client's active view also switches the client to it (the record is written and `active_view_changed` is broadcast), so the user sees what the agent arranged.
- **Document ops.** `open`, `focus`, `split`, `close`, and `move` are applied by the shell to the client's layout of the view: the file is read (materialized from the seed of the client's device kind when the client has none), edited by the pure editor (`shell/dockview_document.py`), written, and `layout_updated` is broadcast; a project view files an opened address into its tab set and a close runs the referenced-instance cleanup, exactly as a browser's save does. Placement follows the document's tree, never the screen: the anchor is the requester's own chat panel when the document holds it, else the document's active group, else its first group; a direction finds the nearest enclosing branch of the matching orientation and the sibling on that side, and tabs into that group unless `--new-group`; a split takes `--ratio` of the anchor group's own extent. A launcher (New Tab) panel in the document counts as empty and is dropped from the group an op docks into.
- **Creates.** `open <address> [--action <id>] [--param name=value]...` (and `split` of the same forms): `app:<name>` of an app with instances runs the create through the shell's relay inside the op, `--action` or the app's `default_shortcut.action` or its first declared action with every `--param`, then docks the record it made; the app's refusal (a `400`, `409`, or `503`) is the op's error, verbatim. `app:<name>?instance=<key>` docks a listed instance (`404` when nothing lists it). A bare `https://` or `http://` URL means `open app:browser --action new --param url=<url>`. A bare word that is an app name means `app:<word>`. An `open` of an address the document already shows focuses it. An op names the address it made in its answer, which `open` prints to stdout.
- **Transient verbs.** `maximize`, `restore`, `refresh`, and `reload_system_interface` change what is on screen without changing the saved document, so they alone still travel as a `layout_op` message (section 8) to the resolved client's windows; `refresh app:<name>` and the interface reload go to every window.
- **Answers.** A document op answers `{"ok", "view_id", "client_id", "layout", "created_address"?}` with `layout` in the shape `inspect` prints, so the script prints its diff from the answer and exits; nothing polls.
- `rename <address> <title>` and `delete <address>` call the shell's relay routes; `replace-url <address> <path-or-url>` calls the relay's location route.
- `list` reads `GET /api/inventory` and prints, per app: `name`, `display_name`, `is_running`, `actions`, and `instances` with `key`, `address`, `title`, `status`, `docked_in` (client ids; `--view` narrows it to clients whose active view is that view). `views` reads the same document and prints every view with `tabs` (addresses) and `clients` (ids with device kind); `context` prints every client with `active_view`, `device_kind`, `is_connected`, and recent activity. `shortcuts` for Everything derives the fixed rail from the inventory's apps.
- Exit codes stay `0`, `1`, `3`.
- The old spellings (`chat:`, `terminal:`, `service:`, `url:`, `subagent:`, `chat-terminal:`) are errors that name the new form.

## 13. Deep links

Honoured by the shell on page load for the requesting client, then stripped from the URL:

- `?view=<view_id>`: switch to the view.
- `&open=<address>`: dock or focus the instance in that view.
- `&action=<app>:<action_id>`: run the action.
- `&follow=<client_id>`: reserved; ignored in this arc.

Unknown or stale targets are ignored silently.
The browser applies these itself through the paths a click takes, before it reports its first `client_state`; the `view` wins over the stored active view.
The minds chrome does not yet forward a deep link's query to the shell frame; that lands with the switcher (deferred, see the meta spec).

## 14. Tool environments

- The manifest is the discriminator: every directory under `system/apps/` with both a `pyproject.toml` and an `app.toml` is a Python app that runs from its own uv tool: `uv tool install -e system/apps/<package> [--with-editable <plugin path>]...`, with the plugin list from `system/config/mngr_plugins.toml` where the app's manifest `name` appears in a plugin's `tools`.
- A directory with a `pyproject.toml` and no manifest is a pre-manifest app (scaffolded before this arc); it keeps running `uv run <name>` from the root venv, untouched by the build and the apply. There are exactly these two forms, both supported indefinitely (phase 9 decided against rewriting a user's app during an update), so no code path ever handles a third.
- The tool's entry point is named after the program and is what the supervisord line runs.
- `system/scripts/build_workspace.sh` loops over the manifest directories.
- The update apply reinstalls the tool of every manifest app whose directory changed in the merge (excluding paths under `frontend/` and `static/`), and of every manifest app when a shared backend manifest changed, and snapshots the tool directory of every `critical` app before it does.
- `system/apps/*` stays in the root workspace's member glob, so one lockfile covers the tree, a pre-manifest app's `{ workspace = true }` source in the root pyproject keeps resolving, and a user-built app needs no root-pyproject edit; `uv sync --all-packages` therefore also installs the manifest apps into the root venv, unused, which uv's shared cache makes nearly free. Membership and the tool environments answer different questions: the lock keeps every environment on the same versions, and a tool environment keeps its app running while the root venv is rewritten or broken.
- The `app_manifest` and `app_instances` libraries are workspace members that apps depend on by path, so a tool install pulls them in editable.
- Services stay in the root venv and keep `uv run <name>`.

## 15. Memory priority

`oom_priority.bands.SERVICE_BANDS` gains `"chat": 25`.
The backstop listener resolves a program's band by finding the registry row whose `program` equals the program name and reading its `priority`; a program with no row, or a row with `priority = "user"`, gets `USER_SERVICE`, and the existing `_NON_SERVICE_PROGRAM_BANDS` table keeps covering the programs that are not apps.
The `oom_tag_service.py <key>` prefix keeps working unchanged for every program line.

## 16. Migration table

See [phase_09_migration.md](phase_09_migration.md); the mapping is the table in section 9 of the meta spec, made exact there. The migration writes the state files of section 7 (the projects document and the per-device seeds, never a client file), the files app's and the terminal app's stores of section 17, and the marker; it runs from bootstrap at every boot and from the update apply, and it never overwrites an output that exists.

## 17. Where app data and machine state live

Everything a program persists goes under `data/` (gitignored, restic-backed), in one of two places, chosen by what the record is about rather than by which program writes it:

- `data/.apps/<name>/`: everything an app persists about the user's things.
  Every app's instance records live here, at `data/.apps/<name>/instances.json`, whatever document shape the app uses (the library's `JsonStoreInstanceSource` for the files app; the terminal's own `{name, title, workdir}` records).
  A terminal's title and starting directory, and a file viewer's folder, are things the user chose, so they belong here even when the instance is backed by state elsewhere.
  The update-app skill treats this directory as the user's real data: verification never writes to it.
- `data/.state/` (a program's own under `data/.state/<name>/`): what a program keeps about this machine and can rebuild, or must not outlive it: the registry (`data/.state/apps.toml`), the terminal's dispatch scripts and pty-to-tab files (`data/.state/terminal/commands/`), and the shell's client layouts and client records (section 7).

A path an app takes on its command line (`--store`, `--state-dir`) defaults to these locations and is overridden only by tests.
