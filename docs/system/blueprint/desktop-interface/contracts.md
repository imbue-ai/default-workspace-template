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
| `launch_paths` | array of tables | no | `[]` | Each `{id, label, path, params?}`; `path` is rooted with one slash (never `//`), at most 2048 characters, carries no query string or fragment, and holds nothing a URL would escape (RFC 3986 path characters only: alphanumerics, `-._~`, the sub-delimiters, `:@`, and `/`); `params` is an optional array of `{name, label, required}` naming query parameters the shell may append. |
| `default_shortcut` | table | no | absent | `{launch = "<id>", mode = "focus" \| "new"}`; `launch` names a declared launch path, or `open` when the app declares none. |
| `launcher_rank` | integer | no | absent | At least 1; the app's place among the launcher's leading tiles. |
| `references`, `scope`, `wiring`, `handles` | | | | Unchanged. |

`instances`, `instances_url`, and `actions` are removed; a manifest that carries them fails to load.
An app with no `launch_paths` has one synthesized launch path, `open`, labelled `Open <display_name>`, at `/`, which the shell adds when it reads the registry.

Built-in manifests:

| App | `critical` | `priority` | `launcher_rank` | `default_shortcut` | `launch_paths` |
|---|---|---|---|---|---|
| `system_interface` | true | `system_interface` | | none | none; `internal = true` |
| `chat` | true | `chat` | 10 | `{launch = "new", mode = "new"}` | `new` ("New Chat", `/new`, params `account_id` optional, `message` optional) |
| `terminal` | true | `terminal` | 40 | `{launch = "new", mode = "focus"}` | `new` ("New Terminal", `/new`, params `workdir` optional) |
| `terminal-pty` | true | `terminal` | | none | none; `internal = true`, `program = "terminal-pty"` |
| `files` | false | `files` | 20 | `{launch = "new", mode = "focus"}` | `new` ("New File Viewer", `/`, params `path` optional) |
| `browser` | false | `browser` | 30 | `{launch = "new", mode = "focus"}` | `new` ("New Browser", `/new`, params `url` optional) |

## 3. The registry (`data/.state/apps.toml`)

Written only by `forward_port.py`.
Each `[[apps]]` row carries `name`, `url`, `label`, `icon`, `internal`, `program` from the registration and `display_name`, `critical`, `priority`, `default_shortcut` (inline table `{launch, mode}`), `launch_paths` (array of inline tables `{id, label, path, params?}` with `params` as the array of names), and `launcher_rank` from the manifest.
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
        {"id": "win-0123456789abcdef", "app": "chat", "path": "/?chat=agent-3f2a", "title": "Plan the launch", "opened_at": "2026-09-19T14:11:02.824Z", "is_settling": false}
      ]
    }
  ]
}
```

- `desktops` is in creation order; the first is the fallback desktop.
- `color` is `#RRGGBB`; `glyph` is `0..9`; `sharing` is `shared` or `personal`; `wallpaper` is `{"kind": "bundled" | "file", "name"}` or `null`, with `name` matching `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`.
- `shortcuts`: `target.kind` is `launch` (the only V1 kind); at most one shortcut per `(app, launch)`; `cell.column` and `cell.row` are integers at least 0.
- `windows` is in opening order; ids are unique across every desktop; `path` and `title` obey section 1; `is_settling` is true from an open at a launch path until the first location report, and false for an open at an explicit path.
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
- A window with no placement reads as `{frame: cascade(n), state: NORMAL, is_minimized: true}` at the start of the list (the bottom of the stack), where `n` is the count of stored placements.
- The old `layouts/` directory is never read.

### 4.3 `clients.json`

`{"version": 2, "clients": {"<client_id>": {"active_desktop": "<desktop_id>", "last_seen": "<RFC 3339>"}}}`.
A version-1 file (with `device_kind` and `active_view`) is read with `active_view` taken as the active desktop when a desktop of that id exists, else the first desktop, and rewritten at version 2 on the next write.

### 4.4 Wallpapers

Bundled wallpapers ship at `imbue/system_interface/static/wallpapers/<name>.<ext>`; file wallpapers live at `data/.apps/system_interface/wallpapers/<name>.<ext>`.
Accepted extensions: `png`, `jpg`, `jpeg`, `webp`.
The `name` of a wallpaper reference is the file name without its extension.

## 5. Routes

All on the shell (`MINDS_WORKSPACE_SERVER_URL`, default `http://127.0.0.1:8000`).
Every error body is `{"detail": "<message>"}`.
`POST /api/client-activity` and `POST /api/layout/broadcast` are loopback-only (`403` otherwise); every other route is for browsers too.

### 5.1 Page and app routes

Unchanged: `GET /` and the SPA catch-all (with `X-Frontend-Built`), `/assets/<path>`, `/favicon.ico`, `GET /api/health`, `GET /_static/app_contract.js`, `GET /api/templates-catalog`, `POST /api/apps/<name>/stop`, `POST /api/apps/<name>/start` (refused for critical apps), `POST /api/client-activity`, `/api/ws`.
`POST /api/client-activity` takes `{"client_id", "desktop_id", "kind": "message", "app", "key", "text"}`: the client that sent a message to an app's page, the desktop it was on (from the shell's handshake), the app, the page's marker (a chat id; `""` for a page without one), and the text; the shell appends it to the client-activity log as a `message` event (the text truncated), which is what `layout.py context` and an op's requester attribution read.
Removed: `POST /api/apps/<name>/changed`, `POST /api/apps/<name>/instances` and every `/instances/<key>/...` relay route, `POST /api/tabs/<tab_id>/instance`, every `/api/projects/...` route, `GET` and `POST /api/layouts/<view_id>`.

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

`desktop` is the object of section 4.1.

### 5.3 Windows

| Route | Request | Response |
|---|---|---|
| `POST /api/desktops/<id>/windows` | `{"app", "path", "client_id", "if_present": "focus" \| "new", "launch": "<launch path id>"?}` | `201 {"window", "is_new": true}` for an open; `200 {"window", "is_new": false}` when `if_present` is `focus` and a window of that app at that path exists on the desktop; `400` for an unregistered app or a bad path. `launch` marks the path as a launch path, which sets `is_settling` |
| `POST /api/desktops/<id>/windows/<window_id>/close` | | `204`; idempotent |
| `POST /api/desktops/<id>/windows/<window_id>/location` | `{"path", "title"}` | `200 window`; `404` unknown window; `400` bad path or title |

An open with `is_new` writes the requesting client's placement (section 10, cascade) on top of its stack and broadcasts `placements_updated` for that client, and `desktops_updated` for everyone.
An open answered with `is_new: false` restores and raises the existing window in the requesting client's layout, writes it, and broadcasts `placements_updated` for that client.
A location report on a settling window clears `is_settling`.
A close drops the window from every layout file of the desktop and broadcasts `desktops_updated` and one `placements_updated` per rewritten layout.
A location that changes nothing writes and broadcasts nothing.

### 5.4 Placements

| Route | Request | Response |
|---|---|---|
| `GET /api/placements/<desktop_id>?client=<client_id>` | | `200 layout` (the client's own, else `{"version": 1, "updated_at": null, "placements": []}`) |
| `POST /api/placements/<desktop_id>` | `{"client_id", "save_id", "base_updated_at", "placements"}` | `200 {"updated_at"}`; `null` when the body equalled the stored layout and nothing was written; `409` when the stored `updated_at` is newer than `base_updated_at` |

`layout` is the object of section 4.2.
A save whose placements name windows the desktop does not hold is accepted with those entries dropped.

### 5.5 Clients, inventory, wallpapers

| Route | Response |
|---|---|
| `GET /api/clients` | `{"clients": [{"id", "active_desktop", "last_seen", "is_connected"}]}` |
| `GET /api/inventory` | `{"desktops": [desktop, ...], "apps": [app, ...], "clients": [client with "shown": [window_id, ...]]}` where `shown` is the windows of the client's active desktop that its layout does not minimize |
| `GET /api/wallpapers` | `{"wallpapers": [{"kind", "name", "url"}]}`, bundled first |
| `GET /wallpapers/<kind>/<name>` | the image; `404` otherwise |

`app` is `{"name", "display_name", "icon", "label", "url", "internal", "program", "critical", "launch_paths": [{"id", "label", "path", "params": [name, ...]}], "default_shortcut", "launcher_rank", "is_running"}`.

## 6. The WebSocket

Route `/api/ws`, one connection per browser window.

Inbound: `client_state {"client_id", "active_desktop", "previous_desktop"}` on connect and on every desktop switch; the shell records the active desktop and `last_seen`, and logs a `desktop_switch` activity when `previous_desktop` differs.

Outbound:

| Type | Payload | When |
|---|---|---|
| `apps_updated` | `{"apps": [app, ...]}` | on connect, and when any row or liveness changed |
| `desktops_updated` | `{"desktops": [desktop, ...]}` | on connect, and after any write of `desktops.json` (a desktop, shortcut, wallpaper, window open or close, or location change) |
| `placements_updated` | `{"desktop_id", "client_id", "save_id"}` | after any write of a layout file; a window applies it only when `client_id` is its own, the desktop is the one it shows, and `save_id` is not one it minted |
| `active_desktop_changed` | `{"client_id", "desktop_id"}` | after a `client_state` report or an op changed the client's stored active desktop |
| `layout_op` | `{"op", "args", "requester", "target_client_id"}` | only the transient ops `refresh` and `reload_system_interface` (section 8) |

`is_connected` on a client is whether any window of it holds the socket.

## 7. The app contract (`app_contract.js`)

Served at `/_static/app_contract.js` with `Access-Control-Allow-Origin: *`.
Exports `connectToShell({onHandshake, onShown, onHidden, onCloseRequest, onNavigate, capabilities})` returning `{isFramed, focused(), location(path, title), openPath(path, ifPresent), disconnect()}`.
`openPath` sends `shell:open` below.
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

Following rule: after every `desktops_updated`, for every live page of a window whose stored `path` differs from that page's last reported path, the shell sends `shell:navigate` when the page declared navigation, else reassigns the iframe `src`, and records the stored path as that page's last report at once, so a second broadcast before the page lands does not navigate it again.
A page's own report never navigates it.
A client other than the opener creates no page for a window while `is_settling` is true.

Nested frames: an app page that frames another page of its own origin (the chat root) forwards `minds:` messages from that frame to `window.parent` unchanged, re-posts the inner page's `shell:focused` as its own, and forwards the inner page's `shell:open` of a sub-agent view, from one module named in `test_embed_ratchets.py`'s allowlist.
A `shell:open` whose path is the root's own (`/` or `/?chat=<id>`) it answers itself, by selecting that chat in place, rather than asking the shell for a second root window.
The shell and the minds chrome accept messages only from frames they created, so nothing else reaches them from an inner frame.

## 8. The op route and `layout.py`

`POST /api/layout/broadcast` with `{"op", "args", "requester"}`, loopback only.
`requester` is `{"app", "marker"}` (`{"app": "chat", "marker": "<chat-id>"}` for a chat's agent, from `MINDS_CHAT_ID`, else `MNGR_AGENT_ID`), or `null`; `self` in a window argument names the window of `app` on the target client's active desktop whose path carries `marker` as a path segment or a query value.
Targeting: `args.client`, else the client that most recently messaged the requester, else the one connected client, else `412` listing the connected clients.
`args.desktop` names the desktop an op edits by name or id and switches the target client to it.

| Op | Args | Effect |
|---|---|---|
| `context` | | read-only; every client's recent activity (`{"ok", "clients"}`) |
| `desktops`, `list` | | read-only; the inventory document of section 5.5 (with `"ok"`) |
| `load` | `desktop` | switch the client to the desktop |
| `open` | `app`, `path?`, `launch?`, `params?`, `if_present?` | open a window at `path`, else at the launch path (`launch`, else the app's `default_shortcut.launch`, else its first) with `params` as the query string; a window of the app at that path is focused unless `if_present` is `new`; answers the window id |
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

The compact breakpoint is `COMPACT_MAX_WIDTH_PX = 700` in `theme/metrics.ts`, applied as `matchMedia("(max-width: 700px)")`; touch is `matchMedia("(pointer: coarse)")`.
Colours, type roles, radii, and elevation come from `base.css` and are not repeated here.

## 12. Selectors shared with tests

Data attributes, never classes, so restyling cannot break a test:

| Attribute | On |
|---|---|
| `data-desktop-id` | the backdrop root |
| `data-window-id`, `data-window-state`, `data-minimized` | each window's root |
| `data-drag-handle`, `data-resize-edge="n\|s\|e\|w\|ne\|nw\|se\|sw"` | title bar, resize edges |
| `data-window-control="minimize\|maximize\|restore\|close\|menu"` | the controls |
| `data-shortcut="<app>:<launch>"`, `data-cell="<column>,<row>"` | each shortcut |
| `data-taskbar`, `data-taskbar-entry="<window-id>"`, `data-launcher-field`, `data-launcher-overlay` | the taskbar and launcher |
| `data-tray-widget="desktops\|running-apps"`, `data-desktop-switch="<id>"` | the tray |
| `data-live-page="<window-id>"` | each iframe |
| `data-launch="<app>:<launch>"` | launcher tiles (today's `data-launch` spelling kept) |

## 13. Where data lives

Unchanged from the workspace app model's section 17: `data/.apps/<name>/` for what an app persists about the user's things (now including `data/.apps/system_interface/wallpapers/`), `data/.state/<name>/` for what a program keeps about this machine (the registry, the shell's desktops, placements, and clients).
