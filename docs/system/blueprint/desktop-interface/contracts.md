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
| `launch_paths` | array of tables | no | `[]` | Each `{id, label, path, method?, params?, presets?, text_param?, draft_param?}`; `path` is rooted with one slash (never `//`), at most 2048 characters, carries no query string or fragment, and holds nothing a URL would escape (RFC 3986 path characters only: alphanumerics, `-._~`, the sub-delimiters, `:@`, and `/`); `method` is `GET` (the default: the page itself, with the params as its query) or `POST` (the shell posts the params and answers with the page to open; post-launch-paths plan section 3); `params` is an optional array of `{name, label, required}` naming the parameters; `presets` is an optional table of fixed string name-value pairs sent with every launch, whose names may not repeat a param's; `text_param` optionally names one of the params as the one the launcher fills with typed text, and `draft_param` as the one it fills with text to be drafted, either of which makes the launch path a free-text row of the launcher (launcher-and-getting-started plan section 3.1); at most one of the two. The names `client_id`, `desktop_id`, and `window_path` are the shell's envelope and are refused as param or preset names. |
| `default_shortcut` | table | no | absent | `{launch = "<id>", mode = "focus" \| "new"}`; `launch` names a declared launch path, or `open` when the app declares none. |
| `launcher_rank` | integer | no | absent | At least 1; the app's place among the launcher's leading tiles. |
| `pin` | table | no | absent | `{path, style = "plain" \| "avatar", scope = "linked" \| "independent", default_mode = "bar" \| "floating"}`; `path` obeys the launch path rule; a registered, non-internal app then has exactly one pinned window on every desktop (pinned-taskbar-entries plan section 7.1). |
| `window_closed_path` | string | no | absent | A path shaped like a launch path; the shell posts every closed window of the app there (section 5.3), for an app whose resources live as long as their windows (`docs/system/specs/window-bound-resources.md`). |
| `references`, `scope`, `wiring`, `handles` | | | | Unchanged. |
| `preview` | table | no | the scaffold convention | How a throwaway instance boots for a preview (`PreviewSpec`; the workspace app model's contracts section 2 has the field-by-field rule): `command` (default: the program as its console script), `ports` (named free ports; `main` always), `env`, `args`, `copies` (repo-relative directories copied into the instance's scratch space, by key), `health_path` (default `/health`), `open_path` (default `/`), `open_path_takes_key`. `command`, `args`, and `env` values may carry `{port:<name>}`, `{copy:<key>}`, `{host}`, `{scratch}`, and `{registry}`; `open_path` may carry `{key}` exactly when `open_path_takes_key`. Absent, the table is `env = {<PACKAGE_UPPER>_PORT = "{port:main}", <PACKAGE_UPPER>_HOST = "{host}", <PACKAGE_UPPER>_DATA_DIR = "{copy:data}"}` over `copies = {data = "data/.apps/<name>"}`. |

`instances`, `instances_url`, and `actions` are removed; a manifest that carries them fails to load.
An app with no `launch_paths` has one synthesized launch path, `open`, labelled `Open <display_name>`, at `/`, which the shell adds when it reads the registry.

Built-in manifests:

| App | `critical` | `priority` | `launcher_rank` | `default_shortcut` | `launch_paths` |
|---|---|---|---|---|---|
| `system_interface` | true | `system_interface` | | none | none; `internal = true` |
| `chat` | true | `chat` | 10 | `{launch = "root", mode = "new"}` | `root` ("Chat", `/`, no params); `new` ("New Chat", POST `/api/chats/intake`, params `account_id` optional, `message` optional, presets `target = "new_chat"`, `text_param = "message"`); `send` ("Send to chat...", POST `/api/chats/intake`, param `message` optional, presets `target = "chat_selector"`, `text_param = "message"`); `draft` ("Draft into chat", POST `/api/chats/intake`, param `message` optional, presets `target = "current_chat"`, `is_draft = "true"`, `draft_param = "message"`) |
| `getting-started` | false | `getting-started` | 5 | `{launch = "open", mode = "focus"}` | none; the shell synthesizes `open` ("Open Getting Started", `/`) |
| `terminal` | true | `terminal` | 40 | `{launch = "new", mode = "new"}` | `new` ("Terminal", POST `/new`, params `workdir` optional) |
| `terminal-pty` | true | `terminal` | | none | none; `internal = true`, `program = "terminal-pty"` |
| `files` | false | `files` | 20 | `{launch = "new", mode = "new"}` | `new` ("File Viewer", `/`, params `path` optional) |
| `browser` | false | `browser` | 30 | `{launch = "new", mode = "focus"}` | `new` ("Browser", POST `/new`, params `url` optional) |

The chat manifest also declares `[pin] path = "/", style = "avatar", scope = "independent", default_mode = "floating"`.
The critical built-ins declare their `[preview]` tables (the workspace app model's contracts section 2 tabulates them): the shell boots `system-interface --preview --state-dir {copy:state}` over a copy of `data/.state/system_interface` with `MINDS_APPS_FILE = "{registry}"`; the chat `chat-app --secondary` over a copy of `data/.apps/chat` (`CHAT_DATA_DIR`), opening on `/?chat={key}`; the terminal `terminal-app --no-register` over a copy of `data/.apps/terminal` and a `{scratch}` state dir with `MINDS_APPS_FILE = "{registry}"`, booted `--with terminal-pty`; and the pty `terminal-pty --no-register` over a `{scratch}` state dir, probed at `/`. Getting Started, though not critical, declares one too, since its entry point registers itself: `getting-started --no-register --state-dir {scratch}/state`, which registers nothing and opens no first-visit window.

## 3. The registry (`data/.state/apps.toml`)

Written only by `forward_port.py`.
Each `[[apps]]` row carries `name`, `url`, `label`, `icon`, `internal`, `program` from the registration and `display_name`, `critical`, `priority`, `default_shortcut` (inline table `{launch, mode}`), `launch_paths` (array of inline tables `{id, label, path, method?, params?, presets?, text_param?, draft_param?}` with `params` as the array of names, `method` written when the manifest gives it, and `presets` only when non-empty), `launcher_rank`, `pin` (inline table `{path, style?, scope?, default_mode?}`, each absent key reading as the manifest's default), and `window_closed_path` from the manifest.
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
      "wallpaper": {"kind": "bundled", "name": "apricot-coast"},
      "shortcuts": [
        {"target": {"kind": "launch", "app": "chat", "launch": "new"}, "mode": "new", "cell": {"column": 0, "row": 0}}
      ],
      "windows": [
        {"id": "win-0123456789abcdef", "app": "chat", "path": "/?chat=agent-3f2a", "title": "Plan the launch", "opened_at": "2026-09-19T14:11:02.824Z", "is_pinned": false, "scope": "linked"}
      ]
    }
  ]
}
```

- `desktops` is in creation order; the first is the fallback desktop.
- `color` is `#RRGGBB`; `glyph` is `0..9`; `wallpaper` is `{"kind": "bundled" | "file", "name"}` or `null`, with `name` matching `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`.
- `shortcuts`: `target.kind` is `launch` (the only V1 kind); at most one shortcut per `(app, launch)`; `cell.column` and `cell.row` are integers at least 0.
- `windows` is in opening order; ids are unique across every desktop; `path` and `title` obey section 1; `is_pinned` (default `false`) marks the app's pinned window, and `scope` (default `linked`) is `linked` or `independent` (pinned-taskbar-entries plan section 3.2). An independent window's `path` stays its home path.
- A file whose `version` is not 1, or that fails validation, is logged and treated as absent: the shell then creates the default desktop. The old `projects.json` is never read. A desktop that still carries the retired `sharing` key is read with the key dropped, and so is a window that still carries the retired `is_settling` key.

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

`{"version": 2, "clients": {"<client_id>": {"active_desktop": "<desktop_id>", "last_seen": "<RFC 3339>", "user_id": "<user_id>" | null, "entries": {"<app>": {"mode": "bar" | "floating", "style": "plain" | "avatar", "position": {"x": 0.9, "y": 0.85} | null}}}}}`.
`user_id` is the signed-in visitor the client last arrived as (section 5.5), null for the owner or an anonymous client; an entry without the key reads as null. `entries` (default `{}`) is how the client shows each pinned entry.
A version-1 file (with `device_kind` and `active_view`) is read with `active_view` taken as the active desktop when a desktop of that id exists, else the first desktop, and rewritten at version 2 on the next write.

### 4.3a `users.json`

`{"version": 1, "users": {"<user_id>": {"desktop_id": "<desktop_id>", "desktop_name": "<name>", "email": "<email>" | null, "display_name": "<name>" | null, "last_seen": "<RFC 3339>"}}}`.
One entry per signed-in visitor the shell has made a desktop for (plan section 3.10): the desktop and its name as of the user's last arrival (a rename is picked up by the next arrival), and the identity as of that arrival.
A user id matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, as the identity header carries it.

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
Presence (the share identity spec, `specs/share-identity-and-presence/spec.md` in the mngr repository, section 4.7): `POST /api/presence/heartbeat` takes no body and reads the requester's `X-Imbue-Identity` header (`204` and nothing recorded when it carries no `user_id`; otherwise it touches the mtime of `data/.state/presence/users/<user_id>.json`, writing the file with `{"user_id", "email", "owner", "first_seen"}` on first sight, and answers `{"identity": {"owner", "user_id", "email"}}`); a user is connected while that mtime is within 70 seconds, and the file is never removed. `GET /api/presence` answers `{"users": [present_user, ...]}`, one entry per connected user: `user_id`, `email`, `display_name`, `profile_picture_url`, `owner`, `first_seen`, `last_seen` (the mtime). `display_name` and `profile_picture_url` are the account's profile as imbue_cloud holds it (`null` when it has none or cannot be reached): fetched from `GET {SHARE_BROKER_URL}/users/<user_id>/profile` (public; answers `{"user_id", "display_name", "profile_picture_url"}`), the broker named in `data/.secrets/share.env`, with a 2 second bound, and cached -- answers and failures alike -- for 5 minutes under `data/.state/presence/profiles/<user_id>.json`. There is no leave route: a page that goes away simply stops heartbeating.
`POST /api/client-activity` takes `{"client_id", "desktop_id", "kind": "message", "app", "key", "text"}`: the client that sent a message to an app's page, the desktop it was on (from the shell's handshake), the app, the page's marker (a chat id; `""` for a page without one), and the text; the shell appends it to the client-activity log as a `message` event (the text truncated), which is what `layout.py context` and an op's requester attribution read.
Removed: `POST /api/apps/<name>/changed`, `POST /api/apps/<name>/instances` and every `/instances/<key>/...` relay route, `POST /api/tabs/<tab_id>/instance`, every `/api/projects/...` route, `GET` and `POST /api/layouts/<view_id>`.
Added: `GET /api/updates/pending` (`200` with the update notice -- the rollback point the last `update_self.py apply --keep-rollback-point` kept, whose fields the workspace app model's contracts section 5 lists -- or `null` when none is kept); `POST /api/updates/pending/confirm` (runs `update_self.py confirm-last`; `204`; `409` when none is kept or while a rollback runs; `500` naming a failed script); `POST /api/updates/pending/rollback` (starts `update_self.py rollback-last` detached, its output going to `data/.state/update-apply/rollback-last.log`, and answers `202` once the script has written its first progress into the record, so a second window's press reads that progress rather than starting a second script; `409` when none is kept, one is already running, the point was already taken back, or the script refused, in its own words; `500` when the script could not be started or wrote no progress within 30s). An unknown path under `/api/` answers `404 {"detail": "No such API route: /<path>"}` rather than the app shell.
A preview shell (`system-interface --preview`, booted by `preview_app.py` over a seeded copy of the state directory and a copied registry) answers `POST /api/apps/<name>/stop`, `POST /api/apps/<name>/start`, and the two notice verbs with `403 {"detail": "This is a preview of a proposed change; it cannot change the live workspace."}`; every other route edits its own copy or reaches only its own windows, so it stays live. Its page carries the meta tag `system-interface-preview` (content `true`), which the frontend reads to offer no Stop or Start, and never the staleness tag.

### 5.2 Desktops

| Route | Request | Response |
|---|---|---|
| `GET /api/desktops` | | `{"desktops": [desktop, ...]}` |
| `POST /api/desktops` | `{"name", "color", "glyph"}` | `201 desktop`, seeded shortcuts, no windows, wallpaper `null`; `409` on an id conflict |
| `POST /api/desktops/<id>/settings` | `{"name", "color", "glyph"}` | `200 desktop` |
| `POST /api/desktops/<id>/wallpaper` | `{"wallpaper": wallpaper \| null}` | `200 desktop`; `404` when the named wallpaper does not exist |
| `POST /api/desktops/<id>/delete` | | `200 {"fallback_desktop_id"}`; `409` for the last desktop |
| `POST /api/desktops/<id>/shortcuts` | `{"target", "mode", "cell"}` | `200 desktop`; replaces the entry for the same `(app, launch)`; `400` for an app or launch path the registry does not declare |
| `POST /api/desktops/<id>/shortcuts/move` | `{"app", "launch", "cell"}` | `200 desktop`; an occupant of the cell is moved to the nearest free cell (section 10) |
| `POST /api/desktops/<id>/shortcuts/remove` | `{"app", "launch"}` | `200 desktop` |

`desktop` is the object of section 4.1, each window carrying one field the file does not store: `client_paths`, the path each client's page of an independent window is at, by client id (`{}` for a linked window; a client at the home path has no entry). A reader of the shell's windows (`docs/system/specs/window-bound-resources.md` section 4.2) takes `path` and every `client_paths` value alike, since any client's view of a window keeps what it shows alive. The `window` objects the window routes answer (section 5.3) carry it too.

### 5.3 Windows

| Route | Request | Response |
|---|---|---|
| `POST /api/desktops/<id>/windows` | `{"app", "path", "client_id", "if_present": "focus" \| "new"}` | `201 {"window", "is_new": true}` for an open; `200 {"window", "is_new": false}` when `if_present` is `focus` and a window of that app at that path exists on the desktop; `400` for an unregistered app or a bad path |
| `POST /api/desktops/<id>/launch` | `{"app", "launch", "params"?, "client_id", "target": {"kind": "new" \| "focus" \| "window", "window_id"?}, "minimized"?}` | Runs a launch path (post-launch-paths plan section 5): the page is the launch path with its presets and `params` as the query for a GET, and for a POST what the app answers to `{presets..., params..., "client_id", "desktop_id", "window_path"}` posted to it. `new` and `focus` then open as the windows route does (`201`/`200 {"window", "path", "is_new"}`); `window` points the named window at the page as a location report for `client_id` would (`200`, `is_new` false), with the window's path as that client sees it as `window_path`. `400` for an unregistered app, an unknown launch path, an undeclared param, a window of another app, or a launch the app refused (a 4xx from it carrying a `detail`: `<app> refused the launch: <detail>`); `502` when the app could not be asked (unreachable, timed out, or a 4xx with no `detail`) or answered no path |
| `POST /api/desktops/<id>/windows/<window_id>/close` | | `204`; idempotent; `409` for a pinned window |
| `POST /api/desktops/<id>/windows/<window_id>/location` | `{"path", "title", "client_id"}` | `200 window`; `404` unknown window; `400` bad path or title. For an independent window the report is stored for `client_id` alone and the answer's `path` and `title` are that client's |

An open with `is_new` writes the requesting client's placement (section 10, cascade) on top of its stack and broadcasts `placements_updated` for that client, and `desktops_updated` for everyone.
An open answered with `is_new: false` restores and raises the existing window in the requesting client's layout, writes it, and broadcasts `placements_updated` for that client.
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

### 5.5 Clients, arrival, inventory, wallpapers

| Route | Response |
|---|---|
| `GET /api/clients` | `{"clients": [{"id", "active_desktop", "last_seen", "is_connected", "user_id", "entries"}]}` |
| `POST /api/clients/<client_id>/arrive` | `{"desktop_id", "created_desktop": desktop \| null, "replaced_desktop_name": string \| null}` |
| `POST /api/clients/<client_id>/entries/<app>` | takes `{"mode", "style", "position"}` (section 4.3), for a pinned non-internal app, the style `plain` or the pin's; answers the client record and announces `client_entries_changed` to that client's windows |
| `GET /api/avatars` | `{"designs": [{"id", "label", "source_path"}], "selected", "default"}` |
| `POST /api/avatars` | loopback only: registers `{"id", "label", "svg", "source_path"}` (`201`); `400` for a design off the vocabulary of `docs/system/avatar-designs.md` |
| `GET /api/avatars/<id>/image.svg?mood=idle\|working&preview=1` | the rendered image; `GET /api/avatars/<id>/source.svg` the original as an attachment; `404` otherwise |
| `POST /api/avatar-selection` | takes `{"design"}`; writes `avatar_selection.json` and announces `avatar_selection_changed`; `400` for an unknown design |
| `GET /api/inventory` | `{"is_preview", "desktops": [desktop, ...], "apps": [app, ...], "clients": [client with "shown": [window_id, ...]]}` where `is_preview` is whether a preview shell (section 5.1) answered and `shown` is the windows of the client's active desktop that its layout does not minimize. The one read a shell page boots from (below), and what an agent's `desktops` and `list` ops answer (section 8) |
| `GET /api/wallpapers` | `{"wallpapers": [{"kind", "name", "url"}]}`, bundled first |
| `GET /wallpapers/<kind>/<name>` | the image; `404` otherwise |

`app` is `{"name", "display_name", "icon", "label", "url", "internal", "program", "critical", "launch_paths": [{"id", "label", "path", "method", "params": [name, ...], "presets": {name: value, ...}, "text_param", "draft_param"}], "default_shortcut", "launcher_rank", "pin", "is_running"}`, `pin` the manifest table or `null`, and each launch path's `text_param` and `draft_param` the declared param name or `null`.

The arrival is what a shell page posts first, with its client id; it reads the requester's `X-Imbue-Identity` header (the share identity spec, section 4.2). The page then reads `GET /api/inventory` once, taking the desktops (the seeded one among them), the apps, and its own client record from that one answer, so it knows every app before it draws a shortcut and needs nothing from the socket to show a complete desktop; the socket's `apps_updated` and `desktops_updated` (section 6) carry every change from then on. Until the inventory answers, a shortcut whose app the page cannot look up draws as connecting (`data-connecting="true"`) and running it says the page is still connecting, rather than that the app is not registered.
`desktop_id` is where the client lands (`null` while the workspace has no desktop): for the owner and for a request with no `user_id`, the client's stored desktop when it exists, else the first desktop (section 4.3); for a visiting user (`owner` false with a `user_id`), the desktop made for them.
On a visiting user's first arrival the shell creates that desktop and answers it as `created_desktop`: named after the user (their profile's `display_name` as section 5.1 resolves it, else the local part of their `email`, else `Guest`; suffixed ` 2`, ` 3`, ... until neither the name nor its id is taken), with the first free glyph and that glyph's colour, holding the first desktop's shortcuts, its wallpaper, and one new window at the path of each of its settled windows; it is recorded in `users.json`, broadcast as `desktops_updated`, and the client is recorded on it with its `user_id`.
A later client of the same user lands on that desktop; a returning client keeps the desktop it was on, when it last arrived as that same user (a client whose record names another user, or none, lands on the user's desktop).
When the recorded desktop no longer exists the shell seeds another the same way and answers the deleted one's name (as of the user's last arrival) as `replaced_desktop_name`, which the page shows once (`data-replaced-desktop-notice`).

## 6. The WebSocket

Route `/api/ws`, one connection per browser window.

Inbound: `client_state {"client_id", "active_desktop", "previous_desktop"}` on connect and on every desktop switch; the shell records the active desktop and `last_seen`, and logs a `desktop_switch` activity when `previous_desktop` differs.

Outbound:

| Type | Payload | When |
|---|---|---|
| `apps_updated` | `{"apps": [app, ...]}` | on connect (a resync: the page's first app list is the inventory's, section 5.5), and when any row or liveness changed |
| `desktops_updated` | `{"desktops": [desktop, ...]}` | on connect, and after any write of `desktops.json` (a desktop, shortcut, wallpaper, window open or close, or location change) |
| `placements_updated` | `{"desktop_id", "client_id", "save_id"}` | after any write of a layout file, and after a write of a client's window path (with a shell-minted save id); a window applies it only when `client_id` is its own, the desktop is the one it shows, and `save_id` is not one it minted |
| `active_desktop_changed` | `{"client_id", "desktop_id"}` | after a `client_state` report, an op, or an arrival (section 5.5) changed the client's stored active desktop |
| `layout_op` | `{"op", "args", "requester", "target_client_id"}` | only the transient ops `refresh` and `reload_system_interface` (section 8) |
| `client_entries_changed` | `{"client_id", "entries"}` | to that client's windows, after its entry presentations were written |
| `avatar_status` | `{"mood": "idle" \| "working", "is_stale"}` | on connect, and when either changes |
| `avatar_selection_changed` | `{"design"}` | after the selection is written |
| `update_notice_changed` | `{"notice": notice \| null}` | on connect (after `avatar_status`), and whenever `data/.state/update-apply/last-good.json` is written or removed and reads differently: an apply kept it, a rollback's progress and outcome, a confirm cleared it; `notice` is the document `GET /api/updates/pending` answers (section 5.1) |
| `presence_updated` | `{"users": [present_user, ...]}` | on connect, when a heartbeat brings a user into the connected set, and when the shell's sweep (every 10 seconds) finds that a user's heartbeats have stopped (section 5.1) |

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
| `open` | `app`, `path?`, `launch?`, `params?`, `if_present?`, `minimized?`, `beside?` | open a window at `path`, else at the page of the launch path (`launch`, else the app's `default_shortcut.launch`, else its first): a GET launch path with `params` as the query string, a POST launch path posted `params` for the page it answers (section 5.3, with the targeted client as `client_id`, or none when the open is unplaced); a window of the app at that page is focused unless `if_present` is `new`; with `minimized`, a window this open creates is placed minimized and one it finds is left as placed; with `beside` (a window argument, resolved as the window verbs resolve one), the named window is set `SNAPPED_LEFT` and the opened one `SNAPPED_RIGHT` and on top, for the target client alone, each keeping its own frame, and a `beside` naming no window on the desktop leaves the opened window as placed rather than refusing the open; answers the window id |
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
Document ops are applied to the files and answered with `{"ok", "desktop_id", "client_id", "desktop", "layout", "window_id"?}`; the two transient ops travel as `layout_op`.
`split`, `move`, and every instance verb (`rename`, `delete`, `stop`, `start`, `replace-url`) are refused with an error naming the replacement; `chat:`, `terminal:`, and `app:` spellings are refused with an error saying to give an app name and a path.
Exit codes are `0`, `1`, `3`.

## 9. Deep links

Honoured by the shell on page load for the requesting client, then stripped: `?desktop=<id>` switches to it; `&open=<app>:<path>` opens (or focuses) a window there; `&launch=<app>:<launch_id>` runs a launch path through the launch route (section 5.3) with a `new` target.
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
| `data-shortcut="<app>:<launch>"`, `data-cell="<column>,<row>"`, `data-connecting="true"` | each shortcut; the last only while its app is unknown because no app list has landed |
| `data-taskbar`, `data-taskbar-entry="<window-id>"`, `data-launcher-field`, `data-launcher-overlay` | the taskbar, the launcher field, and its menu (the overlay's name kept from the tiles) |
| `data-tray-widget="desktops"`, `data-desktop-switch="<id>"`, `data-tray-widget="presence"`, `data-presence-user="<user-id>"`, `data-presence-self="true"` | the tray; the Presence widget is drawn only while two or more users are connected, and `data-presence-self` marks the viewer's own entry, drawn last |
| `data-replaced-desktop-notice="<name>"` | the notice that a visitor's desktop was deleted and replaced |
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
