# The desktop interface: V1

This is the spec for turning the system interface from a tabbed dock into a desktop: desktops that hold windows and shortcuts over a wallpaper, a taskbar with a launcher and a system tray, and apps that own what is inside them.
It is written for the people and agents implementing it, and it is the reference the implementation is judged against.
The vocabulary is [concepts.md](concepts.md) beside this file, which also records the decisions this spec builds on; the exact schemas, routes, messages, and file formats are in [contracts.md](contracts.md).
Implementation happens in this repository as the ordered phases of section 14, each leaving the repository green, with a hard cutover: the old layout files are ignored, and migration is deferred.

## 1. Purpose and principles

A minds workspace is an operating system whose programs are web servers.
The system interface is its window manager: it keeps desktops, places windows, and manages apps, and it knows nothing about what any app shows beyond the app, the path, and the title of each window.
Everything that concerns chats, terminal sessions, folders, and browsers lives inside the app that owns it.

The five principles of the workspace app model decide every question below: one owner per fact; the shell is generic and names no app; truth is shared while arrangement is scoped; minimal shell state; no two-phase commits.
Two more are specific to this design:

6. **The URL is the state.** A window is an app and a path, the page reports where it is, and every client follows. An app that keeps its state in its URL is shareable with no further work.
7. **Rendering never rewrites truth.** Fitting a window to a smaller backdrop, placing a shortcut whose cell is off the grid, and forcing a window maximized in compact mode are all read-time policies; only the user's own gesture writes a frame or a cell.

## 2. Glossary

The full definitions are in [concepts.md](concepts.md); this table is the vocabulary code and documentation use.

| Term | Meaning |
|---|---|
| Desktop | A named, shared collection of windows and shortcuts, with a wallpaper and a sharing mode |
| Window | One app page on one desktop: app, path, title; shared |
| Placement | One client's frame, state, and minimized flag for one window |
| Layout | One client's ordered placements for one desktop; the order is the stack |
| Backdrop | The wallpaper and shortcut grid behind a desktop's windows |
| Shortcut | A grid cell on the backdrop that runs a launch path |
| Launch path | A path under an app's origin that the manifest declares as a way to start something |
| Taskbar | The bottom bar: launcher field, window entries, system tray |
| Launcher | The taskbar's text field and the overlay it opens |
| Tray widget | One component of the system tray; V1: Desktops, Running apps |
| Live page | The one iframe a client keeps for one window |
| Compact mode, touch mode | Render policies from viewport width and pointer type |

Retired: project, view, Everything, tab, panel, pane, instance (in the shell), address, dock (both senses), rail, New Tab page, seed layout, device kind.

## 3. The model

### 3.1 Apps and launch paths

An app is what it is today: one supervisord program, one `app.toml`, one registry row written by `forward_port.py` when the program starts, one origin.
The manifest loses `instances` and `instances_url`, and its `actions` become **launch paths**: each has an id, a label, a `path` under the app origin, and optional params the shell appends as query parameters.
An app that declares no launch path has one synthesized by the shell, `open`, labelled `Open <display name>`, at `/`.
`default_shortcut` and `launcher_rank` keep their meaning.
Section 8 has the manifest; contracts.md section 2 has the exact fields.

The inventory is the registry plus liveness, refreshed when the registry file changes and on the liveness sweep, and pushed as `apps_updated`.
It carries no lists of anything inside an app.

### 3.2 Desktops

A desktop is `{id, name, color, glyph, sharing, wallpaper, shortcuts, windows}`, stored in `desktops.json` in creation order.
The id is the slugified name and never changes; two names that shorten to one id conflict.
`sharing` is `shared` or `personal`; V1 stores and shows it and enforces nothing, since the workspace has one user.
A fresh workspace, and a workspace whose state directory holds no `desktops.json`, gets one desktop named `Home` with the theme's default wallpaper and the seeded shortcuts of 3.6.
It is created on the first read of the desktops after the inventory has read the registry once, so its shortcuts are seeded from the apps that are actually registered rather than from an empty registry at boot.
Deleting the last desktop is refused with `409`.
Deleting a desktop closes its windows (their pages are destroyed in every client) and removes every client's layout of it; clients on it switch to the first remaining desktop.

### 3.3 Windows

A window is `{id, app, path, title, opened_at, is_settling}` on exactly one desktop, in `desktops.json` under its desktop, in opening order.
`is_settling` is true from an open at a launch path until the page's first location report: while it is true, the stored path is the launch path (with its query string), and a client other than the opener creates no page for the window, so a launch path that creates something runs once (section 4.6).
The id is `win-<16 hex>`, minted by the shell when the window is opened.
`path` obeys the rule an instance URL obeyed: a single leading slash, at most 2048 characters, no control characters.
`title` is what the page last reported, trimmed, at most 256 characters; empty means "show the app's display name".

Windows are shared: open and close change `desktops.json` and broadcast `desktops_updated`, and every client of that desktop sees the window come and go.
Path and title are shared and live: a page reports them through the contract (section 7), the client hosting that page posts them to the window's location route, the shell stores them and broadcasts, and every other client whose page for that window is not already there navigates it (section 4.6).

A window may show any path of its app, including one that another window of the same app shows.
Two windows at one path are two pages.

### 3.4 Placements and layouts

A layout is one client's ordered list of placements for one desktop, in `placements/<desktop_id>/<client_id>.json`.
A placement is `{window_id, frame, state, is_minimized}`, with `frame` in fractions of the backdrop (`x`, `y`, `width`, `height`, each in `0..1`, the frame wholly inside the unit square), `state` one of `NORMAL`, `SNAPPED_LEFT`, `SNAPPED_RIGHT`, `MAXIMIZED`, and `is_minimized` orthogonal.
The list order is the stack, last on top.
The focused window is the last placement in the list that is not minimized; a layout with no such placement has no focused window and the backdrop has focus.

A layout may lack a placement for a window that exists, and it may hold a placement for a window that no longer exists.
A missing placement reads as `{frame: cascade, state: NORMAL, is_minimized: true}` at the bottom of the stack (the start of the list), and a stale placement is dropped on read and on the next save.
"The most recently focused window of an app" in a layout means the window of that app nearest the top of the stack, minimized or not, since raising moves a placement to the top and minimizing leaves it where it is.
Nothing writes a placement until this client's own gesture does, except the open that this client itself requested (section 4.1), which writes it at once.

Saves work as today: the browser writes its own gestures with a save id and the stamp it was based on, a save over a newer stored file is refused with `409` and the window refetches, the shell writes agent ops itself, and every write is broadcast as `placements_updated` with the owning client id.

### 3.5 Clients

A client is `{id, active_desktop, last_seen}` in `clients.json`; the device kind is gone.
The active desktop is stored on the server so a client resumes where it was and its windows mirror each other.
A client unseen for 90 days is dropped with every layout it owns, by the sweep that runs at shell start and daily.
A client whose active desktop no longer exists is moved to the first desktop on its next report.

### 3.6 Shortcuts and the grid

A shortcut is `{target, mode, cell}` where `target` is `{kind: "launch", app, launch}` (the only V1 kind), `mode` is `focus` or `new`, and `cell` is `{column, row}` with both at least zero.
`focus` raises the most recently focused window of that app in this client's layout of this desktop (restoring it when minimized) and runs the launch path only when there is none; `new` always runs it.
A desktop holds at most one shortcut per `(app, launch)`.

A new desktop is seeded from every registered, non-internal app's `default_shortcut`, in registry order, laid out in reading order from the grid origin.
The grid is `columns = max(1, floor((width - inset) / cell_width))` by `rows = max(1, floor((height - inset) / cell_height))` over the backdrop, with the cell size and inset from the theme (contracts.md section 10).
Rendering places every shortcut that has a free cell inside the grid at its cell, then every other shortcut at the nearest free cell to its clamped cell (Euclidean distance in cell units, ties by column then row); no two shortcuts ever draw in one cell, and nothing is written.
Dragging a shortcut writes its cell; dropping it on an occupied cell moves the occupant to the nearest free cell (that occupant's cell is written too).

### 3.7 Wallpaper and backdrop

A wallpaper is `{kind: "bundled", name}` or `{kind: "file", name}` or `null`.
Bundled wallpapers ship in the shell's static assets; file wallpapers are image files under `data/.apps/system_interface/wallpapers/` (a user or an agent drops them there; upload is deferred), listed by `GET /api/wallpapers` and served by `GET /wallpapers/<kind>/<name>`.
The backdrop draws the wallpaper with `cover` fit, centred, over the theme's backdrop colour, and the grid over that.
`null` draws the theme's default wallpaper.

### 3.8 Ownership

| Fact or verb | Owner |
|---|---|
| Which apps exist, their display name, icon, launch paths, criticality, priority | Manifest, mirrored into the registry |
| Whether an app is running; Stop and Start | Shell, via supervisord |
| What is inside an app, and its own verbs on those things | The app, in its pages |
| Desktops: name, colour, glyph, sharing, wallpaper, shortcuts and cells | Shell, shared |
| Which windows a desktop holds; each window's path and title | Shell, shared; the page reports path and title |
| Each client's placements and stacking order; the active desktop | Shell, per client |
| Taskbar entries, tray widgets, the launcher's contents | Derived in the browser |
| Workspace-level facts minds needs | Minds, through its own contract, untouched |

### 3.9 Invariants

- The shell imports nothing from `imbue.mngr` or any app, never runs the `mngr` binary, names no app, and has no vocabulary for what is inside an app. The existing ratchets (`test_project_ratchets.py`) hold, and the instance vocabulary leaves the package.
- The shell's state lives under `data/.state/system_interface/` and `data/.apps/system_interface/`; it reads and writes nothing under an mngr directory.
- The only app the shell needs to boot and render is itself. With no other app registered, a desktop shows its wallpaper and an empty launcher.
- A page's iframe is created once per window per client and is never re-parented; only a window's close, or its desktop's deletion, destroys it.
- Rendering never writes: no frame, cell, or state is rewritten by a fit, a clamp, or a compact-mode override.
- `postMessage` and `message` listeners exist only in the contract module, the shell's relay, the embed module, and an app's own declared relay module (the chat root page's), enforced by `test_embed_ratchets.py`.

## 4. Behaviour

### 4.1 Opening a window

Every open goes through one route, `POST /api/desktops/<id>/windows` with `{app, path, client_id, if_present, launch?}`, whatever asked for it: a shortcut, a launcher tile, a page's `shell:open`, an agent's `layout.py open`, a deep link, or the chat app's auto-open of a chat started outside the workspace.
`if_present` is `focus` (the default) or `new`: with `focus`, a window of that app at that exact path already on the desktop is answered instead of opened, and the shell restores and raises it in the requesting client's layout, so the rule holds for an agent op with no browser connected just as it does for a click.
A shortcut in `focus` mode asks the client's own layout first (any window of the app, most recently focused) and opens only when there is none.

The shell mints the id, appends the window to the desktop, writes the **requesting client's** placement at once (state `NORMAL`, not minimized, frame from the cascade rule, on top of that client's stack), and broadcasts `desktops_updated` and `placements_updated` for that client.
Every other client has no placement and shows the window minimized in its taskbar.
An agent op names the client it targets, which is the requesting client for this purpose.

The cascade rule: the frame is the theme's default window size in fractions, at the cascade origin stepped once per window this client already has placed on the desktop, cycling after six.
In compact mode the frame is written the same way and rendered maximized.

The requesting client's live page for the window is created at the app origin plus the path (plus query params for a launch path) as soon as the placement lands.
Other clients create the page when the window is first restored there, and keep it from then on.

### 4.2 Focus, stacking, and clicking into pages

The top non-minimized window is focused.
Raising moves a placement to the end of the list and saves.
A click on a window's chrome raises it.
A click into a page cannot reach the shell across origins, so the pages of every window but the focused one are inert (`pointer-events: none` on their iframes), the press lands on a transparent shield over the window, and the shell raises it and makes its page interactive; the first press is consumed.
A page that receives keyboard focus by other means reports `shell:focused`, and the shell raises its window.

### 4.3 Moving, resizing, snapping, maximizing, minimizing

The title bar is the drag handle.
Dragging a `NORMAL` window moves its frame; the frame is clamped so that at least the theme's minimum visible width of the title bar stays inside the backdrop, and never above its top.
Dragging a `SNAPPED_*` or `MAXIMIZED` window un-snaps it after the release distance: the window becomes `NORMAL` at its kept frame's size, hung under the pointer at the same horizontal fraction across the title bar it was grabbed at.
Releasing a drag with the pointer within the edge threshold of the left or right backdrop edge snaps the window to that half; within the threshold of the top edge maximizes it; the frame is untouched so restore returns to it.
While a drag is inside a zone, the shell draws a translucent preview of the zone.

Resizing is by the eight edges and corners of a `NORMAL` window, clamped to the theme's minimum size; resizing a snapped or maximized window first un-snaps it.
Double-clicking the title bar, the maximize button, and the restore button toggle `MAXIMIZED` and `NORMAL`.
The minimize button and a taskbar click on the focused window set `is_minimized`; a taskbar click on a minimized window clears it and raises.
Minimizing keeps the page alive and hidden; the page receives `shell:hidden`, and `shell:shown` on restore.
During any gesture every iframe is inert, so a drag across another window's page keeps its pointer events.

### 4.4 Fitting to the backdrop

Rendering multiplies a frame by the backdrop size, then enforces the theme's minimum window size in pixels, then nudges the title bar into view.
A `SNAPPED_LEFT` window renders at `{0, 0, 0.5, 1}`, `SNAPPED_RIGHT` at `{0.5, 0, 0.5, 1}`, and `MAXIMIZED` at `{0, 0, 1, 1}` of the backdrop.
The backdrop is the viewport less the taskbar.
A viewport resize re-renders every window from the same fractions, so windows scale with the minds app and the arrangement returns exactly when it grows back; the shell writes nothing on resize.

### 4.5 Closing

Close removes the window from the desktop for every client: the shell drops it from `desktops.json`, drops it from every layout of that desktop, destroys every client's page for it, and broadcasts.
The close button, the window menu, a taskbar entry's context menu, an agent's `close`, and the minds chrome's close chord (`minds:close-active-tab`, which closes the focused window after sending it `shell:close-request`) all do this.
Nothing else happens to the app: the shell has no notion of stopping or deleting what the window showed.

### 4.6 Following the URL

A page reports `shell:location {path, title}` whenever its path or title changes and once after it loads.
The client hosting the page posts the report to the window's location route; the shell stores both, broadcasts `desktops_updated`, and every client compares each of its live pages' last reported path with the stored one.
A page whose last report equals the stored path is left alone, which is how the driving client's own report never bounces back.
A page that differs is navigated: `shell:navigate {path}` when the page declared, in its `shell:capabilities`, that it handles navigation, else by reassigning the iframe's `src`.
Either way the client records the stored path as that page's last report at once, so a broadcast that arrives before the page has landed does not navigate it twice; the page's own report then confirms it, or, if the page ended up elsewhere, replaces the stored path through the location route.
The title needs no navigation and updates the title bar and taskbar entry live.

A launch path with params opens the page at the path plus the query string; the page's first report replaces it with wherever the page ended up (`/new?message=...` becomes `/?chat=<id>`), the window's stored path follows, and the window stops settling.
Until then only the opening client has a page for the window: another client shows its taskbar entry dimmed and defers a restore until the window settles, and a reload of the opener's page while settling runs the launch path again, which is accepted.

### 4.7 First visit and windows opened elsewhere

A client that switches to a desktop it has no layout for renders every window minimized in the taskbar and nothing on the backdrop but shortcuts.
A window opened by another client, or by an agent targeting another client, appears the same way, in the taskbar, until this client restores it.
A desktop's windows therefore never move anything under a user's pointer except through that user's own actions.

### 4.8 Desktops: switching, creating, settings, deleting

Switching is a client-state report; the client's pages for the previous desktop stay alive and hidden.
The Desktops widget's menu creates a desktop (`New desktop`, minting `Desktop <n>` and the next glyph), opens its settings (name, colour, glyph, wallpaper, sharing), and deletes it (confirmed; refused for the last one).
Creating switches the creating client to the new desktop.

### 4.9 Shortcut gestures

Single click or tap selects; double click, Enter, or Space runs; a drag beyond the threshold lifts the icon, shows the target cell, and drops it there; right-click or long-press opens the shortcut menu (Open, the complementary mode, Remove).
Adding a shortcut: the launcher's tiles and the Running apps popover offer "Add to desktop" for each launch path, placed at the first free cell in reading order.

### 4.10 The taskbar

Left to right: the launcher field; one entry per window of the active desktop in opening order (icon and title, minimized entries dimmed, the focused entry marked); the system tray.
Entry click: restore and raise when minimized, minimize when focused, raise otherwise.
Entry context menu: Restore or Minimize, Maximize or Restore, Close.
The tray's widgets are Desktops and Running apps (concepts.md 2.8); each is one component with one popover, and adding a third is adding a component to a list.
The taskbar is always visible in V1; auto-hide is deferred.

### 4.11 The launcher

The launcher field is a text input at the taskbar's left.
Focusing it opens the overlay above it; typing filters.
Resting content is today's New Tab page with its instance rows replaced by windows: the search field is the taskbar field itself; "Open new" is one tile per launch path of every non-internal app, ranked apps first; "On this desktop" lists the active desktop's windows by title, minimized ones marked; "Start something" and "Start from a template" are unchanged, seeding a chat through the launch path that declares a `message` param.
Search results are windows across every desktop (title and app name, switching desktop on choice), launch paths, intents, and templates.
A choice opens a window on the active desktop and closes the overlay; Escape and a click outside close it.
The overlay is never persisted and never a window.

### 4.12 Compact mode and touch mode

Compact mode is on while the viewport is narrower than the compact breakpoint; touch mode is on while the primary pointer is coarse.
Both are `matchMedia` subscriptions that set `data-compact` and `data-touch` on the root element; every style keys off those attributes, and every behaviour reads the same two flags, so there is one source for each.

Compact: every window renders `MAXIMIZED` whatever its placement says, and the placement is not rewritten; the taskbar uses the compact height and shows icons only; the launcher field collapses to an icon that expands over the entries when tapped; the grid uses the compact cell size; window drag, resize, and snapping are off while shortcut drag stays on; the maximize and restore controls are hidden; minimize, close, and the taskbar are how windows are switched.
Touch: hit targets are at least the theme's touch target size; long-press replaces right-click on windows, entries, and shortcuts; hover-revealed controls are always shown; resize handles are hidden; the inert-page rule and the shield work unchanged, since touch fires pointer events.
A phone is both; a narrow desktop window is compact only; a touch laptop is touch only.

### 4.13 Keyboard

Escape closes the launcher, any popover, and any menu.
Enter and Space run a focused shortcut or taskbar entry.
The close chord is the minds chrome's and arrives as `minds:close-active-tab`.
Window cycling and keyboard move and resize are deferred.

## 5. The shell backend

### 5.1 State files

Under `data/.state/system_interface/`: `desktops.json`, `placements/<desktop_id>/<client_id>.json`, `clients.json`, and the client-activity event log as today.
Under `data/.apps/system_interface/`: `wallpapers/`.
The old `projects.json`, `layouts/`, and `migrated.json` are ignored and left in place; a `CLEANUP:` note names them for deletion once migration lands.
The boot-time `migrate_workspace_layouts.py` is removed from bootstrap and the apply, and deleted.
Exact shapes: contracts.md section 4.

### 5.2 Routes and the WebSocket

The full tables are contracts.md sections 5 and 6.
In brief: desktops (list, create, settings, delete, shortcuts, wallpaper), windows (open, close, location), placements (read, save), clients, inventory, wallpapers, the app-level Stop and Start, the templates catalog, client activity, health, the contract module, the op route, and the socket carrying `apps_updated`, `desktops_updated`, `placements_updated`, `active_desktop_changed`, and `layout_op`.
Gone: every `/_instances` relay route, `/api/tabs/<id>/instance`, `/api/apps/<name>/changed`, `/api/projects/*`, `/api/layouts/*`.

### 5.3 The pure document editor

`shell/desktop_document.py` replaces `dockview_document.py`: pure functions over the frozen `Desktop`, `Window`, `Layout`, and `Placement` models that both the routes and the agent ops use: open, close, focus, minimize, restore, maximize, snap, place, move a shortcut, and the cascade, fit, and nearest-free-cell rules.
Every rule that the frontend also applies (cascade, fit, nearest free cell) is written once in Python and once in TypeScript against the same constants and the same test vectors (a JSON file of cases both suites read), so the two never disagree.

### 5.4 Agent ops

`system/scripts/layout.py` keeps its shape (one op per call, posted to the loopback-only op route, applied by the shell to the files, answered with the resulting state) and speaks the new verbs; contracts.md section 8 lists them.
Targeting is unchanged: `--client`, else the client that most recently messaged the requester, else the one connected client, else `412`.
`--desktop <name>` names the desktop an op edits and switches the client to it.
`self` resolves to the window of the requester's app whose path carries the requester's marker.

### 5.5 Recovery and updates

The not-built placeholder is unchanged.
The update apply's post-restart probes change from "every critical app's instances API" to "every critical app's `/api/health`", since no app serves an instances API any more.

## 6. The frontend architecture

### 6.1 Modules and layering

`system/apps/system_interface/frontend/src/`, in strict layers, lowest first:

- `theme/`: the token file `default.css` and `metrics.ts`, which reads the metrics behaviour needs from the computed tokens once at boot and on theme change.
- `model/`: the TypeScript mirrors of the shell's records (`Desktop`, `Window`, `Placement`, `Layout`, `Shortcut`, `AppRecord`, `Client`) and the wire parsers.
- `geometry/`: pure functions with no DOM: `frames.ts` (fractions to pixels, fit, nudge, min size, snap zones, cascade), `grid.ts` (cells, nearest free cell, reading order), `stack.ts` (raise, focus, the missing-placement default).
- `reducers/`: pure state transitions over one `DesktopState` record: every verb of section 4 as `(state, event) -> state`, plus the URL-following decision.
- `store/`: the one implementation class, `DesktopStore`, holding the client's state (desktops, apps, the active desktop's layout, live pages, the gesture in progress, the open overlay), applying reducers, saving with debounce and conflict handling, and subscribing to the socket. Nothing else holds mutable state.
- `pages/`: the live-page layer (`livePages.ts`), keyed by window id: create once, position, show and hide, make inert, navigate, destroy.
- `gestures/`: `pointerGestures.ts`, the one module that listens to pointer events for window drag, resize, and shortcut drag, behind a `GestureSource` interface that yields `{begin, move, end}` with deltas, so it can be swapped for interact.js without touching a reducer.
- `views/`: Mithril components, each a pure function of records and callbacks: `Backdrop`, `ShortcutIcon`, `Window`, `TitleBar`, `WindowMenu`, `Taskbar`, `TaskbarEntry`, `LauncherField`, `LauncherOverlay` (hosting today's New Tab sections), `SystemTray`, `DesktopsWidget`, `RunningAppsWidget`, `DesktopSettingsDialog`, `SnapPreview`, and the shared `Popover` and `Menu` (today's `placeMenu` rule, moved).
- `relay.ts`, `embed.ts`: unchanged.
- `App.ts`, `index.ts`: wiring.

`import-linter`-style ordering is enforced by a vitest test that walks imports, as `lint-and-format.test.ts` does today.

### 6.2 The store and reducers

`DesktopStore` is the only class with mutable fields.
A user gesture or a socket message becomes an event; the store applies the reducer, gets a new state, schedules a redraw, and schedules a save when the reducer marked the layout dirty.
Reducers are the unit-test surface: every rule of section 4 has a vitest case, and the shared JSON vectors of 5.3 are run through them.
Two desktops on one page cost nothing, since a store is an instance.

### 6.3 Components: style versus behaviour

A component receives records and callbacks and renders markup with Tailwind utilities over the semantic token layer, as the frontend style guide says.
It attaches no pointer listeners for gestures; it renders the elements that `pointerGestures.ts` binds to by data attribute (`data-drag-handle`, `data-resize-edge`, `data-shortcut`).
It reads no metric from the DOM; the store hands it what it needs from `metrics.ts`.
So a theme is a token file, a look is a component, and a behaviour is a reducer plus a gesture binding, and each can change alone.

### 6.4 Live pages

One iframe per window per client, keyed by window id, created when the window is first shown in this client and destroyed only when the window closes or its desktop is deleted.
The layer sits above the backdrop and below the window chrome in z-order per window, so the chrome's resize edges and shield stay clickable over a cross-origin page; the reconcile step positions each page over its window's content box.
Hidden pages (minimized, on another desktop) are `display: none`, which is what a hidden dockview tab did.
Sandbox and permission attributes are today's.
The handshake, shown, hidden, close-request, and navigate messages go through `relay.ts` as today.

### 6.5 Gestures

`pointerGestures.ts` uses pointer capture, a start threshold, and the `GestureSource` interface.
It makes every iframe inert for the gesture's duration and restores the focused page's interactivity after.
Touch needs nothing extra beyond `touch-action: none` on handles.

### 6.6 Theme and metrics

`theme/default.css` extends `base.css` with the desktop tokens of contracts.md section 11.
`metrics.ts` reads the ones behaviour needs (title bar height, taskbar heights, cell sizes, inset, minimum window size, minimum visible title width, snap threshold, drag threshold, touch target size) from `getComputedStyle(document.documentElement)` once at boot and again on `data-compact` or `data-touch` change, and hands the store a frozen `ThemeMetrics`.
No metric is a literal in TypeScript, and the compact breakpoint is the one exception in the other direction: it is a TypeScript constant applied as a `matchMedia` query that sets `data-compact`, and CSS keys off the attribute, so it too lives once.

## 7. The app contract (v2)

The module stays `system/libs/workspace_ui/src/app_contract.ts`, served at `/_static/app_contract.js`, imported by every app page.
Trust is unchanged: a page accepts only `window.parent`, the shell accepts only frames it created within the workspace origin family.
The messages, exactly, are contracts.md section 7; in brief:

Shell to page: `shell:handshake {clientId, windowId, desktopId, path}` after every load and when the window's desktop changes; `shell:shown`, `shell:hidden`; `shell:close-request`; `shell:navigate {path}`.

Page to shell: `shell:capabilities {navigation: bool}` once after connecting; `shell:location {path, title}`; `shell:focused`; `shell:open {path, ifPresent}` to open another window of the same app on this desktop.

The `connectToShell` signature gains `onNavigate` and a `capabilities` argument, `location(path, title)` takes the title, and `openPath(path, ifPresent)` sends the path form of `shell:open` beside `open(address)`, which sends the address form until phase 6 deletes it.
A page that gives no `onNavigate` is reloaded by `src` when it must follow.
A page that reports no title is titled by the app's display name.

The embedder relay (`relay.ts`) is unchanged: `minds:` messages from any frame the shell created go up, and messages from the chrome go to every such frame.
A page that nests a further frame of its own (the chat root, section 9.1) relays for it, in one declared module the embed ratchet allows.

## 8. The manifest

`app.toml` keeps `name`, `display_name`, `icon`, `critical`, `priority`, `program`, `internal`, `default_shortcut`, `launcher_rank`, `references`, `scope`, `wiring`, and `handles`.
It drops `instances` and `instances_url`.
`actions` becomes `launch_paths`: `[[launch_paths]] id, label, path, params`, where `path` is a path under the app origin and `params` is the documented list of query parameter names the shell may append.
`default_shortcut.action` becomes `default_shortcut.launch`, naming a declared launch path id or `open`.
`forward_port.py` copies the new fields onto the registry row and drops the old ones; the app watcher and minds read `name`, `url`, `label`, `icon` as before.

## 9. The built-in apps

Every built-in loses its instances API, its nudge, and, where it had one, its sidecar; the `app_instances` library is deleted in the last phase.
Each keeps its pages, its own state, and its own verbs, and adopts the contract: report path and title, handle navigate where it can.

### 9.1 Chat

The chat app serves three kinds of page on its origin:

- `/` is the **chat root**: the chat list on the left (Gleb's rail, moved into the chat frontend: grouping of helper agents under their lead chat, status dots, rename, stop and start, delete, the account chooser, new-chat rows for chats still waiting for an account), and an inner iframe on the right showing the selected chat's page. The selection is the query parameter `chat`, so the root's path is `/?chat=<chat-id>`, which is what it reports as its location, with the selected chat's display name as the title. With no selection the root shows its list and an empty slot.
- `/new` is the root with a chat just created and selected (the `new` launch path; `message` and `account_id` params as today); the root reports `/?chat=<id>` once the chat exists.
- `/<chat-id>` is one chat and nothing else, exactly today's chat page, for direct launches (an agent's `open chat /<id>`, minds deep links, the inner frame); `/<chat-id>.<agent-id>.<session-id>` is a sub-agent view, also as today.

The root and the chat page share an origin, so the root drives the inner frame directly: it sets its `src`, reads its document title, and forwards `shell:shown` and `shell:hidden` into it by calling into its window rather than by messaging.
The root carries one declared relay module: `minds:` messages the inner page posts to its parent go up to the shell, and `shell:focused` from the inner page becomes the root's own `shell:focused`.
The root handles `shell:navigate` by changing the selection, never by reloading.
Presence reporting keys on the chat the inner page shows, as it does today for a chat page.

Sub-agent views open as their own window through `shell:open {path: "/<chat>.<agent>.<session>"}`; the chat page keeps its present behaviour there.
`auto_open.py` posts an `open` op with `{app: "chat", path: "/?chat=<id>"}` so a chat started from outside lands with the list beside it.
The instances API, the provisional-instance phases as a shell concept, `POST /api/apps/chat/changed`, and the `subagent` action are removed; the chat keeps its provisional records internally for its own list.
Client-activity reports on send continue, with the client id from the handshake.

### 9.2 Terminal

ttyd cannot serve a launch path or report a location, so the terminal becomes two registered programs: `terminal` serves a small wrapper page on the app origin, and `terminal-pty`, `internal = true`, is ttyd on its own origin.
The wrapper at `/?session=<name>` embeds `https://<terminal-pty origin>/?arg=_&arg=session&arg=<name>[&arg=<workdir>]` in an inner frame, reports its path and the session name as its title, and handles `shell:navigate` by re-pointing the inner frame.
`/new[?workdir=]` allocates the lowest free `terminal-<N>`, creates the tmux session, and redirects to `/?session=terminal-<N>`.
The wrapper posts the `ttyd-focus` message into its inner frame when the shell grants focus, as the shell does today.
Session switching inside tmux is no longer reported to the shell; a reload reattaches to the session in the URL.
The store of remembered terminals, their recreation at startup, and the dispatch scripts stay as they are.

### 9.3 Files

The files app is dufs plus its location beacon; the sidecar goes.
Its one launch path is `new` at `/[?path=]`, which dufs already serves, and the beacon reports the folder path and the folder's name as the title.
There is nothing to navigate in-app, so it declares no navigation capability and is reloaded when it must follow.

### 9.4 Browser

The browser daemon keeps its pages at `/?session=<name>` and gains `/new[?url=]`, which creates a browser and redirects.
Its page reports its path and the page title, and handles `shell:navigate` by switching session.
The fleet CLI and the daemon's own routes are untouched.

## 10. Sharing, memory, and updates

Sharing re-renders from the registry as today; the terminal's pty origin is an internal row and is admitted by a workspace-level grant. Narrowing a visitor to the terminal alone is deferred.
Memory shedding keeps its inputs: the chat app retags agents from `shell:shown` and `shell:hidden`, which the chat root forwards to its inner page.
The update apply refreshes tool environments as today and probes every critical app's `/api/health` after the restart.

## 11. What is deleted

`dockview-core`; `DockviewWorkspace.ts`, `Sidebar.ts`, `NewTabLauncher.ts` as a panel (its sections move into `LauncherOverlay`), `IframePanel.ts`, `liveSurfaces.ts` (replaced by `livePages.ts`), `tabMenu.ts`, `tab-rename.ts`, `AllAppsPicker.ts`, `ProjectMembershipDialog.ts`, `ProjectSettingsModal.ts` (replaced by `DesktopSettingsDialog`); the shell's `dockview_document.py`, `layouts.py`, `projects.py`, `instance_relay.py`, the instance fetching in `inventory.py`, and the instance vocabulary in `data_types.py` and `primitives.py`; `migrate_workspace_layouts.py`; the `manage-projects` skill (folded into `manage-layout`, renamed `manage-desktop`); the `app_instances` library and every `/_instances` blueprint in the built-ins; the prototypes' wallpaper binaries are not imported.
The word "dock" leaves every file the change touches.

## 12. mngr-side changes

In the mngr repository, released together with this: the Electron e2e runner (`apps/minds/imbue/minds/desktop_client/e2e_workspace_runner.py`) waits for the dockview wrapper and presses New Tab tiles, and must instead wait for the taskbar and open a chat through the launcher; `apps/minds/docs/` mentions the dockview UI in the overview, design, and glossary; the embed contract is unchanged, and `minds:close-active-tab` now closes the focused window.
A grep for `dockview`, `New Tab`, and `app:chat?instance` in `apps/minds` finds the rest.

## 13. Testing

- **Reducers and geometry**: vitest over pure functions, plus the shared JSON vectors of 5.3, which `desktop_document_test.py` runs too.
- **Backend**: unit tests beside each module; `test_layout_pipeline.py` for every agent op end to end.
- **End to end**: `test_e2e.py` (Playwright, the real bundle, a registry of stub apps that are static pages speaking the contract): every open path, every window gesture and its persistence across reload, snap and un-snap, a second client seeing a window minimized, URL following across two clients with and without in-app navigation, shortcut drag with collision, desktop create, settings, and delete, the launcher's search and tiles, and the Desktops and Running apps widgets. Every scenario runs again at a phone viewport with touch emulation, asserting compact and touch behaviour.
- **Ratchets**: postMessage confinement (now allowing the chat root's relay), the shell names no app, the shell imports no mngr, and a new one: no literal pixel metric in `views/` or `reducers/`.
- **Selectors** the minds e2e runner and these suites share are data attributes on the taskbar, entries, windows, shortcuts, and launcher, listed in contracts.md section 12.

## 14. Phases

One pull request, ordered commits, each leaving the repository green and each verified by hand in a dev workspace before the next.

The order is additive first: the apps learn the new contract and gain their launch paths while the old shell still runs them, then the shell cuts over, then the old machinery is deleted.

1. **Contract v2, additive.** `shell:location` gains `title`, `shell:capabilities` and `shell:navigate` are added, `shell:open` accepts a path beside the address it takes today (`openPath(path, ifPresent)` beside `open(address)`), `connectToShell` gains the new handlers. The current shell ignores what it does not know, and apps keep the address form of `shell:open` until phase 5. Verify: the current shell still frames every app.
2. **Manifest and registry, additive.** `launch_paths` beside `actions` in the manifest library, `forward_port.py` copying both, every built-in manifest declaring its launch paths. Verify: registry rows carry both; nothing else changes.
3. **Apps.** Each built-in gains its pages of section 9 while keeping its instances API: the chat root at `/` and `/new` with the list moved in and the relay module allowlisted; the terminal wrapper and the `terminal-pty` split with `/new`; the browser's `/new`; the files beacon reporting a title; every page reporting path and title and handling navigate where it can. Verify under the old shell: every app still works in its tabs, and each new page works visited directly, on a laptop and on a phone.
4. **The shell backend, new model beside the old.** `desktops.json`, placements, clients without device kind, the pure editor and the shared geometry vectors, the new routes and socket messages, the op route speaking the new verbs. Old routes still served. Verify: the new routes round-trip in `routes_test.py` and `test_layout_pipeline.py`.
5. **The shell frontend.** The layered modules of section 6, the theme file, compact and touch modes, the launcher overlay hosting the New Tab sections, the two tray widgets; dockview and the rail deleted; the shell reads only the new routes; the chat page switches to the path form of `shell:open` (`openPath`). Verify by hand, on a laptop and on a phone: every behaviour of section 4 against the built-ins; a chat from a shortcut, from the launcher with a seeded message, from an agent, and from the minds "Ask an agent" path; a sub-agent view in its own window; two clients following one chat root's selection; two terminals and a reload reattaching; a files window reopening at its folder; a browser window following an agent's navigate.
6. **Deletion.** The shell's instance relay, tab route, projects, layouts, seeds, and migration; every app's instances API, nudge, `subagent` action, and sidecar; the `app_instances` library; the contract module's `open(address)`; `auto_open.py` retargeted to the op route's new `open`; ratchets tightened. Verify: `test_e2e.py` green with stub apps, and a fresh workspace boots to its default desktop.
7. **Tooling, docs, cleanup.** `layout.py` and the `manage-desktop` skill, the README and blueprint docs, the update apply's probes, the changelog entries, and the mngr-side list of section 12 filed as its own PR. Verify: an agent arranges a desktop from a chat with no browser connected, then a browser connects and sees it.

## 15. Deferred

- Migration of pre-V1 projects and layouts into desktops and placements, and deletion of the old state files.
- Theme switching and a settings route; V1 ships one theme.
- Wallpaper upload from the settings dialog; V1 lists files already in the wallpapers directory.
- Taskbar auto-hide; window cycling and keyboard move and resize; a status signal from pages to the taskbar; a window-targeted shortcut kind.
- Enforcing the sharing mode once workspaces have more than one user; presence, avatars, and a multiplayer chat.
- A richer launcher (type-ahead over app contents through an app-declared search route).
- Narrowing a per-app share grant to the terminal alone, which needs its pty origin admitted with it.
- Reporting a terminal's in-tmux session switch as a location, which needs the ttyd client to learn the session name.
- Full-screen mode for a window.
