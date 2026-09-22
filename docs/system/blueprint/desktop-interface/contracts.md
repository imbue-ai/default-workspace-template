# The desktop interface: contracts

Every cross-cutting schema, route, message, file format, and constant that [plan-desktop-interface.md](plan-desktop-interface.md) relies on.
The vocabulary is [concepts.md](concepts.md).
Every rule here is normative; where the plan's prose differs, this file is the truth.
Wire spelling is `snake_case` in JSON bodies and files, and `camelCase` in the contract messages between the shell and a page, as today.

## 1. Identifiers

- An **app name** obeys `forward_port.py`'s rule, unchanged.
- A **desktop id** is the slugified desktop name: `^[a-z0-9][a-z0-9-]{0,127}$`, never changing after creation.
- A **window id** is `win-<16 hex>`, minted by the shell when a window is opened, never reused.
- A **launch path id** matches `^[a-z0-9][a-z0-9-]{0,31}$` and is unique within its manifest; `open` is reserved for the synthesized one.
- A **client id** is the uuid the browser keeps in local storage under `si-client-id`, held to `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`.
- A **save id** is `save-<16 hex>`, minted by a window (browser sense) for each placements save it makes.
- A **path** is a path under an app's origin: a single leading `/` (never `//`), at most 2048 characters, no control characters, query string allowed.
- A **title** is trimmed, at most 256 characters; empty means "use the app's display name".

## 2. The manifest (`app.toml`)

Parsed by `app_manifest` with `extra = "forbid"`.

| Field | Type | Required | Default | Rule |
|---|---|---|---|---|
| `name` | string | yes | | An app name; equals the `--name` at registration. |
| `display_name` | string | yes | | Non-empty, at most 64 characters. |
| `icon` | string | unless `internal` | | `.svg` beside the manifest. |
| `critical` | bool | no | `false` | No Stop verb; snapshot-and-rollback target in the apply. |
| `priority` | string | no | `"user"` | A memory band name or `user`. |
| `program` | string | no | `name` | The supervisord program. |
| `internal` | bool | no | `false` | Hidden from every open surface. |
| `launch_paths` | array of tables | no | `[]` | Each `{id, label, path, params?, text_param?}`; `path` is rooted with one slash (never `//`), at most 2048 characters, carries no query string or fragment, and holds nothing a URL would escape (RFC 3986 path characters only: alphanumerics, `-._~`, the sub-delimiters, `:@`, and `/`); `params` is an optional array of `{name, label, required}` naming query parameters the shell may append; `text_param` optionally names one of them as the param the launcher fills with typed text, which makes the launch path a free-text row of the launcher (launcher-and-getting-started plan section 3.1). |
| `default_shortcut` | table | no | absent | `{launch = "<id>", mode = "focus" \| "new"}`; `launch` names a declared launch path, or `open` when the app declares none. |
| `launcher_rank` | integer | no | absent | At least 1; the app's place among the launcher's leading tiles. |
| `pin` | table | no | absent | `{path, style = "plain" \| "avatar", scope = "linked" \| "independent", default_mode = "bar" \| "floating"}`; `path` obeys the launch path rule; a registered, non-internal app then has exactly one pinned window on every desktop (pinned-taskbar-entries plan section 7.1). |
| `window_closed_path` | string | no | absent | A path shaped like a launch path; the shell posts every closed window of the app there (section 5.3), for an app whose resources live as long as their windows (`docs/system/specs/window-bound-resources.md`). |
| `message_handlers` | array of tables | no | `[]` | Each `{type, path}`: `type` is a message type the app takes, `minds:` and a lowercase kebab-case name (at most 64 characters), unique within the manifest; `path` is a route under the app's origin shaped like a launch path. The shell posts every message of that type from the minds chrome there (section 5.6). |
| `references`, `scope`, `wiring`, `handles` | | | | Unchanged. |
| `preview` | table | no | the scaffold convention | How a throwaway instance boots for a preview (`PreviewSpec`; the workspace app model's contracts section 2 has the field-by-field rule): `command` (default: the program as its console script), `ports` (named free ports; `main` always), `env`, `args`, `copies` (repo-relative directories copied into the instance's scratch space, by key), `health_path` (default `/health`), `open_path` (default `/`), `open_path_takes_key`. `command`, `args`, and `env` values may carry `{port:<name>}`, `{copy:<key>}`, `{host}`, `{scratch}`, and `{registry}`; `open_path` may carry `{key}` exactly when `open_path_takes_key`. Absent, the table is `env = {<PACKAGE_UPPER>_PORT = "{port:main}", <PACKAGE_UPPER>_HOST = "{host}", <PACKAGE_UPPER>_DATA_DIR = "{copy:data}"}` over `copies = {data = "data/.apps/<name>"}`. |

`instances`, `instances_url`, and `actions` are removed; a manifest that carries them fails to load.
An app with no `launch_paths` has one synthesized launch path, `open`, labelled `Open <display_name>`, at `/`, which the shell adds when it reads the registry.

Built-in manifests:

| App | `critical` | `priority` | `launcher_rank` | `default_shortcut` | `launch_paths` |
|---|---|---|---|---|---|
| `system_interface` | true | `system_interface` | | none | none; `internal = true` |
| `chat` | true | `chat` | 10 | `{launch = "root", mode = "new"}` | `root` ("Chat", `/`, params `draft` optional); `new` ("New Chat", `/new`, params `account_id` optional, `message` optional, `text_param = "message"`); `send` ("Send to chat...", `/send`, param `message` optional, `text_param = "message"`) |
| `getting-started` | false | `getting-started` | 5 | `{launch = "open", mode = "focus"}` | none; the shell synthesizes `open` ("Open Getting Started", `/`) |
| `terminal` | true | `terminal` | 40 | `{launch = "new", mode = "new"}` | `new` ("Terminal", `/new`, params `workdir` optional) |
| `terminal-pty` | true | `terminal` | | none | none; `internal = true`, `program = "terminal-pty"` |
| `files` | false | `files` | 20 | `{launch = "new", mode = "new"}` | `new` ("File Viewer", `/`, params `path` optional) |
| `browser` | false | `browser` | 30 | `{launch = "new", mode = "focus"}` | `new` ("Browser", `/new`, params `url` optional) |

The chat manifest also declares `[pin] path = "/", style = "avatar", scope = "independent", default_mode = "floating"` and `[[message_handlers]] type = "minds:focus-chat", path = "/api/focus-chat"`.
The critical built-ins declare their `[preview]` tables (the workspace app model's contracts section 2 tabulates them): the shell boots `system-interface --preview --state-dir {copy:state}` over a copy of `data/.state/system_interface` with `MINDS_APPS_FILE = "{registry}"`; the chat `chat-app --secondary` over a copy of `data/.apps/chat` (`CHAT_DATA_DIR`), opening on `/?chat={key}`; the terminal `terminal-app --no-register` over a copy of `data/.apps/terminal` and a `{scratch}` state dir with `MINDS_APPS_FILE = "{registry}"`, booted `--with terminal-pty`; and the pty `terminal-pty --no-register` over a `{scratch}` state dir, probed at `/`. Getting Started, though not critical, declares one too, since its entry point registers itself: `getting-started --no-register --state-dir {scratch}/state`, which registers nothing and opens no first-visit window.

## 3. The registry (`data/.state/apps.toml`)

Written only by `forward_port.py`.
Each `[[apps]]` row carries `name`, `url`, `label`, `icon`, `internal`, `program` from the registration and `display_name`, `critical`, `priority`, `default_shortcut` (inline table `{launch, mode}`), `launch_paths` (array of inline tables `{id, label, path, params?, text_param?}` with `params` as the array of names), `launcher_rank`, `pin` (inline table `{path, style?, scope?, default_mode?}`, each absent key reading as the manifest's default), `window_closed_path`, and `message_handlers` (array of inline tables `{type, path}`) from the manifest.
`instances`, `instances_url`, and `actions` are no longer written; a row that still carries them (an app not yet re-registered) is read with those keys ignored.
The shell validates every row on read and skips one that fails, with a warning.

## 4. Shell state files

Under `data/.state/system_interface/`, written atomically under one process-wide lock.

### 4.1 `desktops.json`

```json
{
  "version": 1,
  "desktops": [
    {
      "id": "home",
      "name": "Home",
      "color": "#2f6b4f",
      "glyph": 0,
      "sharing": "shared",
      "wallpaper": {"kind": "bundled", "name": "apricot-coast"},
      "shortcuts": [
        {"target": {"kind": "launch", "app": "chat", "launch": "new"}, "mode": "new", "cell": {"column": 0, "row": 0}}
      ],
      "windows": [
        {"id": "win-0123456789abcdef", "app": "chat", "path": "/?chat=agent-3f2a", "title": "Plan the launch", "opened_at": "2026-09-19T14:11:02.824Z", "is_settling": false, "is_pinned": false, "scope": "linked"}
      ]
    }
  ]
}
```

- `desktops` is in creation order; the first is the fallback desktop.
- `color` is `#RRGGBB`; `glyph` is `0..9`; `sharing` is `shared` or `personal`; `wallpaper` is `{"kind": "bundled" | "file", "name"}` or `null`, with `name` matching `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`.
- `shortcuts`: `target.kind` is `launch` (the only V1 kind); at most one shortcut per `(app, launch)`; `cell.column` and `cell.row` are integers at least 0.
- `windows` is in opening order; ids are unique across every desktop; `path` and `title` obey section 1; `is_settling` is true from an open at a launch path until the first location report, and false for an open at an explicit path; `is_pinned` (default `false`) marks the app's pinned window, and `scope` (default `linked`) is `linked` or `independent` (pinned-taskbar-entries plan section 3.2). An independent window's `path` stays its home path.
- A file whose `version` is not 1, or that fails validation, is logged and treated as absent: the shell then creates the default desktop. The old `projects.json` is never read.

### 4.2 `placements/<desktop_id>/<client_id>.json`

```json
{
  "version": 1,
  "updated_at": "2026-09-19T14:12:40.001Z",
  "placements": [
    {"window_id": "win-0123456789abcdef", "frame": {"x": 0.05, "y": 0.06, "width": 0.6, "height": 0.7}, "state": "NORMAL", "is_minimized": false}
  ]
}
```

- `placements` is back to front; last is on top.
- `frame` values are floats in `0..1` with `x + width <= 1` and `y + height <= 1`; `state` is `NORMAL`, `SNAPPED_LEFT`, `SNAPPED_RIGHT`, or `MAXIMIZED`.
- A placement naming a window the desktop no longer holds is dropped on read.
- A window with no placement reads as `{frame: cascade(n), state: NORMAL, is_minimized: true}` at the start of the list (the bottom of the stack), where `n` is the count of stored placements. A pinned window with no placement is answered by the layout route as `{frame: {x: 0.46, y: 0.05, width: 0.5, height: 0.9}, state: NORMAL, is_minimized: true}` at the start of the list instead (pinned-taskbar-entries plan section 3.2), unwritten until the client saves or an op edits that client's layout.
- The old `layouts/` directory is never read.

### 4.3 `clients.json`

`{"version": 2, "clients": {"<client_id>": {"active_desktop": "<desktop_id>", "last_seen": "<RFC 3339>", "entries": {"<app>": {"mode": "bar" | "floating", "style": "plain" | "avatar", "position": {"x": 0.9, "y": 0.85} | null}}}}}`; `entries` (default `{}`) is how the client shows each pinned entry.
A version-1 file (with `device_kind` and `active_view`) is read with `active_view` taken as the active desktop when a desktop of that id exists, else the first desktop, and rewritten at version 2 on the next write.

### 4.4 `window_paths/<client_id>.json`

`{"version": 1, "windows": {"<window_id>": {"path", "title"}}}`: the client's own path and title for each independent window, written by that client's location reports and an agent's `navigate` targeting it; entries for windows since gone are dropped on read, and the file is pruned with the client.

### 4.5 `avatar_selection.json`

`{"version": 1, "design": "<design id>"}`: the workspace's avatar design; absent or unreadable reads as the default (`gummy-seal`). The registered designs live at `data/.apps/system_interface/avatars/catalog.json` (`docs/system/avatar-designs.md`).

### 4.6 Wallpapers

Bundled wallpapers ship at `imbue/system_interface/static/wallpapers/<name>.<ext>`; file wallpapers live at `data/.apps/system_interface/wallpapers/<name>.<ext>`.
Accepted extensions: `png`, `jpg`, `jpeg`, `webp`.
The `name` of a wallpaper reference is the file name without its extension.

## 5. Routes

All on the shell (`MINDS_WORKSPACE_SERVER_URL`, default `http://127.0.0.1:8000`).
Every error body is `{"detail": "<message>"}`.
`POST /api/client-activity` and `POST /api/layout/broadcast` are loopback-only (`403` otherwise); every other route is for browsers too.

### 5.1 Page and app routes

Unchanged: `GET /` and the SPA catch-all (with `X-Frontend-Built`), `/assets/<path>`, `/favicon.ico`, `GET /api/health`, `GET /_static/app_contract.js`, `POST /api/apps/<name>/stop`, `POST /api/apps/<name>/start` (refused for critical apps), `POST /api/client-activity`, `/api/ws`.
`POST /api/client-activity` takes `{"client_id", "desktop_id", "kind": "message", "app", "key", "text"}`: the client that sent a message to an app's page, the desktop it was on (from the shell's handshake), the app, the page's marker (a chat id; `""` for a page without one), and the text; the shell appends it to the client-activity log as a `message` event (the text truncated), which is what `layout.py context` and an op's requester attribution read.
Removed: `POST /api/apps/<name>/changed`, `POST /api/apps/<name>/instances` and every `/instances/<key>/...` relay route, `POST /api/tabs/<tab_id>/instance`, every `/api/projects/...` route, `GET` and `POST /api/layouts/<view_id>`.
Added: `GET /api/updates/pending` (`200` with the update notice -- the rollback point the last `update_self.py apply --keep-rollback-point` kept, whose fields the workspace app model's contracts section 5 lists -- or `null` when none is kept); `POST /api/updates/pending/confirm` (runs `update_self.py confirm-last`; `204`; `409` when none is kept or while a rollback runs; `500` naming a failed script); `POST /api/updates/pending/rollback` (starts `update_self.py rollback-last` detached, its output going to `data/.state/update-apply/rollback-last.log`, and answers `202` once the script has written its first progress into the record, so a second window's press reads that progress rather than starting a second script; `409` when none is kept, one is already running, the point was already taken back, or the script refused, in its own words; `500` when the script could not be started or wrote no progress within 30s). An unknown path under `/api/` answers `404 {"detail": "No such API route: /<path>"}` rather than the app shell.
A preview shell (`system-interface --preview`, booted by `preview_app.py` over a seeded copy of the state directory and a copied registry) answers `POST /api/apps/<name>/stop`, `POST /api/apps/<name>/start`, and the two notice verbs with `403 {"detail": "This is a preview of a proposed change; it cannot change the live workspace."}`; every other route edits its own copy or reaches only its own windows, so it stays live. Its page carries the meta tag `system-interface-preview` (content `true`), which the frontend reads to offer no Stop or Start, and never the staleness tag.

### 5.2 Desktops

| Route | Request | Response |
|---|---|---|
| `GET /api/desktops` | | `{"desktops": [desktop, ...]}` |
| `POST /api/desktops` | `{"name", "color", "glyph"}` | `201 desktop`, seeded shortcuts, no windows, wallpaper `null`; `409` on an id conflict |
| `POST /api/desktops/<id>/settings` | `{"name", "color", "glyph", "sharing"}` | `200 desktop` |
| `POST /api/desktops/<id>/wallpaper` | `{"wallpaper": wallpaper \| null}` | `200 desktop`; `404` when the named wallpaper does not exist |
| `POST /api/desktops/<id>/delete` | | `200 {"fallback_desktop_id"}`; `409` for the last desktop |
| `POST /api/desktops/<id>/shortcuts` | `{"target", "mode", "cell"}` | `200 desktop`; replaces the entry for the same `(app, launch)`; `400` for an app or launch path the registry does not declare |
| `POST /api/desktops/<id>/shortcuts/move` | `{"app", "launch", "cell"}` | `200 desktop`; an occupant of the cell is moved to the nearest free cell (section 10) |
| `POST /api/desktops/<id>/shortcuts/remove` | `{"app", "launch"}` | `200 desktop` |

`desktop` is the object of section 4.1, each window carrying one field the file does not store: `client_paths`, the path each client's page of an independent window is at, by client id (`{}` for a linked window; a client at the home path has no entry). A reader of the shell's windows (`docs/system/specs/window-bound-resources.md` section 4.2) takes `path` and every `client_paths` value alike, since any client's view of a window keeps what it shows alive. The `window` objects the window routes answer (section 5.3) carry it too.

### 5.3 Windows

| Route | Request | Response |
|---|---|---|
| `POST /api/desktops/<id>/windows` | `{"app", "path", "client_id", "if_present": "focus" \| "new", "launch": "<launch path id>"?}` | `201 {"window", "is_new": true}` for an open; `200 {"window", "is_new": false}` when `if_present` is `focus` and a window of that app at that path exists on the desktop; `400` for an unregistered app or a bad path. `launch` marks the path as a launch path, which sets `is_settling` |
| `POST /api/desktops/<id>/windows/<window_id>/close` | | `204`; idempotent; `409` for a pinned window |
| `POST /api/desktops/<id>/windows/<window_id>/location` | `{"path", "title", "client_id"}` | `200 window`; `404` unknown window; `400` bad path or title. For an independent window the report is stored for `client_id` alone and the answer's `path` and `title` are that client's |

An open with `is_new` writes the requesting client's placement (section 10, cascade) on top of its stack and broadcasts `placements_updated` for that client, and `desktops_updated` for everyone.
An open answered with `is_new: false` restores and raises the existing window in the requesting client's layout, writes it, and broadcasts `placements_updated` for that client.
A location report on a settling window clears `is_settling`.
A close drops the window from every layout file of the desktop and broadcasts `desktops_updated` and one `placements_updated` per rewritten layout.
After that, when the window's app registered a `window_closed_path`, the shell POSTs `{"path", "window_id", "desktop_id"}` to the app's `url` plus that path from a thread of its own, with a 2 second timeout, and neither waits for nor acts on the answer; a deleted desktop's windows are posted the same way. An app may take the posted `path` as proof that a window showed the resource it names (the terminal and the browser mark it window-seen before sweeping), while reading what is shown now from `GET /api/desktops`.
A location that changes nothing writes and broadcasts nothing.

### 5.4 Placements

| Route | Request | Response |
|---|---|---|
| `GET /api/placements/<desktop_id>?client=<client_id>` | | `200 layout` (the client's own, else `{"version": 1, "updated_at": null, "placements": []}`) with `window_paths`, the client's stored `{path, title}` by window id for the desktop's independent windows |
| `POST /api/placements/<desktop_id>` | `{"client_id", "save_id", "base_updated_at", "placements"}` | `200 {"updated_at"}`; `null` when the body equalled the stored layout and nothing was written; `409` when the stored `updated_at` is newer than `base_updated_at` |

`layout` is the object of section 4.2.
A save whose placements name windows the desktop does not hold is accepted with those entries dropped; the save body carries no `window_paths` (the client's paths are written by the location route alone, and the save route refuses a body with a field it does not know).

### 5.5 Clients, inventory, wallpapers

| Route | Response |
|---|---|
| `GET /api/clients` | `{"clients": [{"id", "active_desktop", "last_seen", "is_connected", "entries"}]}` |
| `POST /api/clients/<client_id>/entries/<app>` | takes `{"mode", "style", "position"}` (section 4.3), for a pinned non-internal app, the style `plain` or the pin's; answers the client record and announces `client_entries_changed` to that client's windows |
| `GET /api/avatars` | `{"designs": [{"id", "label", "source_path"}], "selected", "default"}` |
| `POST /api/avatars` | loopback only: registers `{"id", "label", "svg", "source_path"}` (`201`); `400` for a design off the vocabulary of `docs/system/avatar-designs.md` |
| `GET /api/avatars/<id>/image.svg?mood=idle\|working&preview=1` | the rendered image; `GET /api/avatars/<id>/source.svg` the original as an attachment; `404` otherwise |
| `POST /api/avatar-selection` | takes `{"design"}`; writes `avatar_selection.json` and announces `avatar_selection_changed`; `400` for an unknown design |
| `GET /api/inventory` | `{"is_preview", "desktops": [desktop, ...], "apps": [app, ...], "clients": [client with "shown": [window_id, ...]]}` where `is_preview` is whether a preview shell (section 5.1) answered and `shown` is the windows of the client's active desktop that its layout does not minimize |
| `GET /api/wallpapers` | `{"wallpapers": [{"kind", "name", "url"}]}`, bundled first |
| `GET /wallpapers/<kind>/<name>` | the image; `404` otherwise |

`app` is `{"name", "display_name", "icon", "label", "url", "internal", "program", "critical", "launch_paths": [{"id", "label", "path", "params": [name, ...], "text_param"}], "default_shortcut", "launcher_rank", "pin", "message_handlers": [{"type", "path"}, ...], "is_running"}`, `pin` the manifest table or `null`, each launch path's `text_param` the declared param name or `null`, and `message_handlers` the row's handlers (`[]` when it registers none). The `apps_updated` push (section 6) carries the same objects.

### 5.6 Embedder messages

`POST /api/embedder-messages` takes `{"type", "client_id", "payload"}`: a message the minds chrome sent a client's shell page, its type, that client, and the message's other fields.
The shell page posts every message the chrome sends it, once, when some app's `message_handlers` names its type, and posts nothing otherwise; it still rebroadcasts every such message to its child frames unchanged (the workspace app model's contracts section 11).
The shell posts `{"type", "client_id", ...payload}` (the message itself, with the client added) to the `url` plus `path` of every registered app whose row names the type, in registry order, with a 10 second timeout, and reads no payload.
It answers `200 {"type", "deliveries": [{"app", "status", "detail"}, ...]}` when every app answered 2xx (`detail` empty); `502` with the same body and a `detail` naming each app that answered otherwise or could not be reached (its `status` then `null`); `404` when no app handles the type; `400` for a type off the manifest rule, a bad client id, or a payload carrying `type` or `client_id`; and a preview shell `403`, its page posting nothing (the workspace app model's contracts section 6).

## 6. The WebSocket

Route `/api/ws`, one connection per browser window.

Inbound: `client_state {"client_id", "active_desktop", "previous_desktop"}` on connect and on every desktop switch; the shell records the active desktop and `last_seen`, and logs a `desktop_switch` activity when `previous_desktop` differs.

Outbound:

| Type | Payload | When |
|---|---|---|
| `apps_updated` | `{"apps": [app, ...]}` | on connect, and when any row or liveness changed |
| `desktops_updated` | `{"desktops": [desktop, ...]}` | on connect, and after any write of `desktops.json` (a desktop, shortcut, wallpaper, window open or close, or location change) |
| `placements_updated` | `{"desktop_id", "client_id", "save_id"}` | after any write of a layout file, and after a write of a client's window path (with a shell-minted save id); a window applies it only when `client_id` is its own, the desktop is the one it shows, and `save_id` is not one it minted |
| `active_desktop_changed` | `{"client_id", "desktop_id"}` | after a `client_state` report or an op changed the client's stored active desktop |
| `layout_op` | `{"op", "args", "requester", "target_client_id"}` | only the transient ops `refresh` and `reload_system_interface` (section 8) |
| `client_entries_changed` | `{"client_id", "entries"}` | to that client's windows, after its entry presentations were written |
| `avatar_status` | `{"mood": "idle" \| "working", "is_stale"}` | on connect, and when either changes |
| `avatar_selection_changed` | `{"design"}` | after the selection is written |
| `update_notice_changed` | `{"notice": notice \| null}` | on connect (after `avatar_status`), and whenever `data/.state/update-apply/last-good.json` is written or removed and reads differently: an apply kept it, a rollback's progress and outcome, a confirm cleared it; `notice` is the document `GET /api/updates/pending` answers (section 5.1) |

`is_connected` on a client is whether any window of it holds the socket.

## 7. The app contract (`app_contract.js`)

Built once, into the shell's static output, and served by every app at `/_static/app_contract.js` from its own origin (the shell serves it too, with `Access-Control-Allow-Origin: *`): a page imports it as a module, and a module import is a fetch without cookies, which the desktop client's forwarder and the share gateway refuse across origins.
Exports `connectToShell({onHandshake, onShown, onHidden, onCloseRequest, onNavigate, capabilities})` returning `{isFramed, focused(), location(path, title), openPath(path, ifPresent), startWithText(text), disconnect()}`.
`openPath` sends `shell:open` below; `startWithText` sends `shell:start-with-text`.
`capabilities` is `{navigation: boolean}` and must agree with the handlers: giving `onNavigate` without `navigation: true`, or `navigation: true` without `onNavigate`, is an error the module throws at connect.

| Direction | Type | Payload |
|---|---|---|
| shell to page | `shell:handshake` | `{"clientId", "windowId", "desktopId", "path"}`; after every `load` of the frame and when the window's desktop changes |
| shell to page | `shell:shown`, `shell:hidden` | `{}` |
| shell to page | `shell:close-request` | `{}` |
| shell to page | `shell:navigate` | `{"path"}`; only to a page that declared `navigation: true` |
| page to shell | `shell:capabilities` | `{"navigation": bool}`; sent once by `connectToShell`; absent means `false` |
| page to shell | `shell:location` | `{"path", "title"}`; the shell remembers the pair as the page's last report and posts it to the window's location route when it differs from the stored one |
| page to shell | `shell:focused` | `{}`; the shell raises the page's window |
| page to shell | `shell:open` | `{"path", "ifPresent"}`; opens a window of the posting frame's own app on the posting window's desktop, with `client_id` the hosting client |
| page to shell | `shell:start-with-text` | `{"text"}`; the shell runs the launcher's primary free-text row with the text (launcher-and-getting-started plan section 3.7), so a page starts a chat without naming the chat app; with no free-text row on the machine the shell notifies and does nothing |

Following rule: after every `desktops_updated`, for every live page of a window whose stored `path` differs from that page's last reported path, the shell sends `shell:navigate` when the page declared navigation, else reassigns the iframe `src`, and records the stored path as that page's last report at once, so a second broadcast before the page lands does not navigate it again.
A page's own report never navigates it.
A client other than the opener creates no page for a window while `is_settling` is true.

Nested frames: an app page that frames another page of its own origin (the chat root) forwards `minds:` messages from that frame to `window.parent` unchanged, re-posts the inner page's `shell:focused` as its own, and forwards the inner page's `shell:open` of a sub-agent view, from one module named in `test_embed_ratchets.py`'s allowlist.
A `shell:open` whose path is the root's own (`/` or `/?chat=<id>`) it answers itself, by selecting that chat in place, rather than asking the shell for a second root window.
The shell and the minds chrome accept messages only from frames they created, so nothing else reaches them from an inner frame.

## 8. The op route and `layout.py`

`POST /api/layout/broadcast` with `{"op", "args", "requester"}`, loopback only.
`requester` is `{"app", "marker"}` (`{"app": "chat", "marker": "<chat-id>"}` for a chat's agent, from `MINDS_CHAT_ID`, else `MNGR_AGENT_ID`), or `null`; `self` in a window argument names the window of `app` on the target client's active desktop whose path, as the target client sees it (its own stored path for an independent window), carries `marker` as a path segment or a query value; `pinned` names the pinned window of `app` on that desktop (`404` when it has none, `400` with no requester).
Targeting: `args.client`, else the client that most recently messaged the requester, else the one connected client, else `412` listing the connected clients; `open` alone, with nothing settling the client, writes the window on `args.desktop` (else the first desktop) with no placement instead, so it reads as minimized for every client, and answers with `client_id` and `layout` null.
`args.desktop` names the desktop an op edits by name or id and switches the target client to it.

| Op | Args | Effect |
|---|---|---|
| `context` | | read-only; every client's recent activity (`{"ok", "clients"}`) |
| `desktops`, `list` | | read-only; the inventory document of section 5.5 (with `"ok"`) |
| `load` | `desktop` | switch the client to the desktop |
| `show` | `app`, `path`, `showing?` | put the app's page at `path` on the client's screen, choosing the window (below); answers the window id and `shown` |
| `open` | `app`, `path?`, `launch?`, `params?`, `if_present?`, `minimized?` | open a window at `path`, else at the launch path (`launch`, else the app's `default_shortcut.launch`, else its first) with `params` as the query string; a window of the app at that path is focused unless `if_present` is `new`; with `minimized`, a window this open creates is placed minimized and one it finds is left as placed; answers the window id |
| `focus` | `window` | restore and raise |
| `minimize`, `restore`, `maximize` | `window` | set the placement accordingly |
| `place` | `window`, `zone` (`left`, `right`, `maximized`) or `frame` (`x,y,width,height`) | set the state, or the frame with state `NORMAL` |
| `close` | `window` | close for everyone |
| `navigate` | `window`, `path` | set the window's path as if its page had reported it; the client's page follows |
| `refresh` | `window` or `app` | transient: reload the page(s) |
| `reload_system_interface` | | transient: reload every window of the shell |
| `shortcuts`, `shortcut set`, `shortcut move`, `shortcut remove` | as section 5.2 | edit the desktop's shortcuts |
| `wallpaper` | `wallpaper` | set the desktop's wallpaper |

`window` is a window id, `self`, or an app name (that app's most recently focused window on the target client's active desktop).
`show` knows nothing of what the path shows. `showing` is a list of the app's other paths that count as already showing it (`path` itself always does). For the target client, every path as that client sees it (its own stored path for an independent window), the first of these that applies:

1. `raised`: a window of `app` at `path` or a `showing` path, on the client's active desktop before any other and nearest the top of the client's stack first, minimized or not, is restored and raised; one on another desktop is raised there and the client switched to that desktop.
2. `navigated`: the shown (not minimized) window of `app` on the active desktop nearest the top of the stack whose path is the same page as `path` (equal before any `?`) is set to `path` as `navigate` sets it (a linked window moves for every client, an independent one for this client) and raised; a window of the app on another page is never repointed.
3. `pinned`: the app's pinned window on the active desktop is set to `path` the same way and restored.
4. `opened`: a window of `app` at `path` is opened on the active desktop for the client, shown and on top.

`show` is answered like the document ops, with `desktop_id`, `desktop`, and `layout` those of the desktop the path is shown on, and `"shown"` one of the four above.
Document ops are applied to the files and answered with `{"ok", "desktop_id", "client_id", "desktop", "layout", "window_id"?}`; the two transient ops travel as `layout_op`.
`split`, `move`, and every instance verb (`rename`, `delete`, `stop`, `start`, `replace-url`) are refused with an error naming the replacement; `chat:`, `terminal:`, and `app:` spellings are refused with an error saying to give an app name and a path.
Exit codes are `0`, `1`, `3`.

## 9. Deep links

Honoured by the shell on page load for the requesting client, then stripped: `?desktop=<id>` switches to it; `&open=<app>:<path>` opens (or focuses) a window there; `&launch=<app>:<launch_id>` runs a launch path.
Unknown or stale targets are ignored silently.

## 10. Geometry rules and constants

The constants below are theme metrics (section 11) unless marked as fixed.
Both editors (`shell/desktop_document.py` and `frontend/src/geometry/`) implement every rule and pass the shared vectors in `docs/system/blueprint/desktop-interface/geometry_vectors.json`, which the implementation phase creates.

- **Backdrop**: the viewport less the taskbar height, in pixels; fractions are relative to it.
- **Cascade** (fixed, in fractions): window `n` (zero-based, cycling every 6) gets `{x: 0.05 + 0.03 * (n mod 6), y: 0.06 + 0.04 * (n mod 6), width: 0.6, height: 0.7}`, then clamped into the unit square.
- **Snap frames** (fixed): left `{0, 0, 0.5, 1}`, right `{0.5, 0, 0.5, 1}`, maximized `{0, 0, 1, 1}`.
- **Fit** (render only): `pixels = fraction * backdrop`; width and height are raised to the minimum window size; then the frame is shifted so that at least the minimum visible title width of the title bar is inside the backdrop horizontally and the whole title bar height is inside vertically, top edge never above 0.
- **Snap zones**: a drag released with the pointer within the snap threshold of the left or right backdrop edge snaps to that half; within the threshold of the top edge maximizes; the top edge wins a corner.
- **Un-snap**: a drag of a snapped or maximized window beyond the un-snap distance makes it `NORMAL` at its kept frame's width and height, positioned so the pointer sits at the same horizontal fraction of the title bar it was pressed at, then clamped.
- **Drag threshold**: a press becomes a drag after the drag threshold; below it, it is a click.
- **Grid**: origin at the inset from the backdrop's top-left; `columns = max(1, floor((backdrop.width - inset) / cell.width))`, `rows = max(1, floor((backdrop.height - inset) / cell.height))`.
- **Nearest free cell** (fixed): among free cells, the one at the least Euclidean distance in cell units from the clamped target cell, ties by lower column then lower row.
- **Reading order** (fixed): `cell(i) = {column: i mod columns, row: floor(i / columns)}`.
- **Placement of shortcuts** (render only): shortcuts whose stored cell is inside the grid and unclaimed take it, in shortcut order; every other shortcut takes the nearest free cell to its clamped stored cell, in shortcut order.
- **Compact override** (render only): every window renders as `MAXIMIZED`.

## 11. Theme tokens and metrics

`frontend/src/theme/default.css`, imported after `base.css`, declares on `:root` and redeclares under `[data-compact]` and `[data-touch]` where a value differs:

| Token | Default | Compact | Touch | Read by `metrics.ts` |
|---|---|---|---|---|
| `--desk-title-bar-height` | `36px` | | `44px` | yes |
| `--desk-taskbar-height` | `48px` | `56px` | `56px` | yes |
| `--desk-cell-width` | `96px` | `80px` | | yes |
| `--desk-cell-height` | `112px` | `96px` | | yes |
| `--desk-grid-inset` | `16px` | `8px` | | yes |
| `--desk-window-min-width` | `320px` | | | yes |
| `--desk-window-min-height` | `240px` | | | yes |
| `--desk-title-min-visible` | `120px` | | | yes |
| `--desk-snap-threshold` | `16px` | | | yes |
| `--desk-unsnap-distance` | `12px` | | | yes |
| `--desk-drag-threshold` | `4px` | | `8px` | yes |
| `--desk-touch-target` | `32px` | | `44px` | yes |
| `--desk-window-radius` | `12px` | `0px` | | no |
| `--desk-window-shadow` | `var(--shadow-overlay)` | | | no |
| `--desk-taskbar-surface` | translucent surface | | | no |
| `--desk-backdrop` | `var(--c-bg)` | | | no |
| `--desk-default-wallpaper` | `url(/wallpapers/bundled/<name>)` | | | no |
| `--desk-icon-size` | `48px` | `40px` | | no |
| `--desk-shortcut-label-shadow` | `0 1px 2px rgb(0 0 0 / 0.6)` | | | no |
| `--desk-floating-entry-size` | `56px` | | | yes |
| `--desk-floating-entry-inset-x` | `16px` | | | yes |
| `--desk-floating-entry-inset-y` | `12px` | | | yes |
| `--desk-resize-edge` | `8px` | | | no |
| `--desk-resize-corner` | `16px` | | | no |
| `--desk-resize-overhang` | `3px` | | | no |
| `--desk-resize-edge-inset` | `calc(var(--desk-resize-corner) - var(--desk-resize-overhang))` | | | no |
| `--desk-launcher-menu-width` | `22rem` | `calc(100% - var(--spacing) * 4)` | | no |

The compact breakpoint is `COMPACT_MAX_WIDTH_PX = 700` in `theme/metrics.ts`, applied as `matchMedia("(max-width: 700px)")`; touch is `matchMedia("(pointer: coarse)")`.
The resize handles are strips of `--desk-resize-edge` overhanging the window's border by `--desk-resize-overhang` (so a press just outside the frame still grabs an edge), inset from the corners by `--desk-resize-edge-inset`; the corners are `--desk-resize-corner` squares over the same overhang.
Colours, type roles, radii, and elevation come from `base.css` and are not repeated here.

## 12. Selectors shared with tests

Data attributes, never classes, so restyling cannot break a test:

| Attribute | On |
|---|---|
| `data-desktop-id` | the backdrop root |
| `data-window-id`, `data-window-state`, `data-minimized` | each window's root |
| `data-window-frame`, `data-window-content` | the window's clipping frame, and the content box its page is laid over |
| `data-drag-handle`, `data-resize-edge="n\|s\|e\|w\|ne\|nw\|se\|sw"` | title bar, resize edges |
| `data-window-control="minimize\|maximize\|restore\|close\|menu"` | the controls |
| `data-shortcut="<app>:<launch>"`, `data-cell="<column>,<row>"` | each shortcut |
| `data-taskbar`, `data-taskbar-entry="<window-id>"`, `data-launcher-field`, `data-launcher-overlay` | the taskbar, the launcher field, and its menu (the overlay's name kept from the tiles) |
| `data-tray-widget="desktops"`, `data-desktop-switch="<id>"` | the tray |
| `data-live-page="<window-id>"` | each iframe |
| `data-launcher-row="launch:<app>:<launch>" \| "window:<window-id>" \| "text:<app>:<launch>"` | each row of the launcher's menu |
| `data-launch="<app>:<launch>"` | each launch-path and free-text row (the tiles' spelling kept) |
| `data-launcher-window="<window-id>"`, `data-minimized="true" \| "false"` | each window row, and whether its window is minimized for this client |
| `data-text-action="primary" \| "secondary"` | the first two free-text rows |
| `data-highlighted="true" \| "false"`, `data-disabled="true"` | each row's highlight; a disabled free-text row |
| `data-key="enter"` | the `Enter` caption on the highlighted row |
| `data-pinned="true\|false"` | each window's root and each taskbar entry |
| `data-pinned-entry="<app>"`, `data-entry-mode="bar\|floating"`, `data-entry-style="plain\|avatar"` | each pinned entry, in the bar or floating |
| `data-floating-entries` | the floating layer |
| `data-mood="working\|idle"`, `data-stale="true\|false"` | each avatar image's button |
| `data-avatar-chooser`, `data-avatar-design="<id>"` | the chooser and its cells |
| `data-menu-item="float\|move-to-taskbar\|style-plain\|style-avatar\|change-avatar"` | the pinned entry's menu rows |

## 13. Where data lives

Unchanged from the workspace app model's section 17: `data/.apps/<name>/` for what an app persists about the user's things (now including `data/.apps/system_interface/wallpapers/` and the registered avatar designs at `data/.apps/system_interface/avatars/catalog.json`), `data/.state/<name>/` for what a program keeps about this machine (the registry, the shell's desktops, placements, clients, each client's window paths, and the avatar selection).
Also under `data/.state/`: every isolated instance's state at `data/.state/isolated-instances/<name>/` (its pids, ports, logs, `copies/<key>/` for the directories its manifest's preview table names, and `scratch/`), beside a preview's registry copy `<name>-preview.registry.toml`; and the update apply's own files at `data/.state/update-apply/` (its in-flight marker, emergency record, and `snapshots/`, plus `last-good.json` and `rollback-last.log` after an apply run with `--keep-rollback-point`; the workspace app model's section 17 has the detail).
