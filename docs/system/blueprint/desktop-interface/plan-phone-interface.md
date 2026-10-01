# The workspace on a phone

Status: implemented. Supersedes `plan-desktop-interface.md` section 4.12 (compact mode); touch mode stays as that section describes it. Contracts this plan changes are called out against `contracts.md` by section. The interactive mock the layout was iterated in lives beside this plan at `phone-mock/mock.html`; it is the reference for measurements, spacing and copy this plan does not restate, and where the two disagree, this plan wins.

## Overview

- Compact mode is the desktop layout squeezed into a phone: a taskbar of icon-and-title chips that overflows past four windows, a landing on bare wallpaper, windows switched by minimize and restore, and every right-click verb unreachable because nothing on a phone right-clicks. The apps inherit the same squeeze: the chat rail collapses to a strip of monograms, the file viewer's table needs 538px, the terminal has no way to raise the keyboard.
- The phone gets its own layout instead of a squeezed desktop: a bottom bar with home, a pill naming the window you are in, and a plus; a home grid of apps; a windows sheet; a start sheet that is the launcher's menu made tappable. It replaces compact mode entirely and is chosen by the viewport's shorter side being under 700px, so rotating a phone keeps it and no iPad gets it.
- Desktops are not a phone concept. The phone lists every window of every desktop in one flat list and never names, switches or edits a desktop. Windows it opens go to the first desktop, unplaced, which is exactly what an agent's unplaced `open` does today: laptop clients find them minimized at the bottom of their taskbar, and browsing on the phone never rearranges anyone's windows.
- The phone's own notion of "the window I am looking at" is per client and stored on the server as a short history, so a reload lands where you left off, the windows sheet can order by recency, and agent ops that target the phone know what it shows.
- The pinned chat window keeps its rules: exactly one, never closed, its avatar in the pill, chats switched inside it from a drawer. Every other window is a full-page view of the app's page with only that page mounted, plus the chat page, which stays mounted because it is the landing page.
- The desktop layout keeps a single new capability from this work: toasts, which replace every `alert()` the shell shows for a failed operation.
- Out of the phone: the presence strip, the element-reference menu, pop-out and solo view, the avatar chooser, desktop settings, minimize, and per-window status dots. Agent notifications stay a studio-app feature, and how a phone reaches the workspace is not this plan's concern.
- In scope beyond the shell: the chat app's phone layout (header, drawer, composer settings button, kebab verbs), a row layout for the file viewer as further patches to the vendored dufs frontend, a key strip and tap-to-raise for the terminal, and correct stream sizing for the browser. Everything lands in one merge; the phases below are for implementation order only.

## Expected behavior

### Choosing the layout

- The phone layout is on while `min(viewport width, viewport height) < 700px`, read as one `matchMedia` query (`(max-width: 700px), (max-height: 700px)`) that sets `data-phone` on the root. `data-compact` is gone. `data-touch` is unchanged and orthogonal.
- Every phone in portrait or landscape gets it; no iPad does (the iPad mini's shorter side is 744px). A desktop browser window under 700px tall gets it too, which is accepted.
- Switching between layouts on resize is live, as compact mode's toggle is today. The desktop layout is untouched apart from the toasts and the removal of compact-only branches.

### The bar

- A bottom bar of three controls: home, the pill, plus. It sits above the safe-area inset and stays visible while the soft keyboard is up. Geometry as the mock: 64px by 42px controls with a circular press highlight, a 48px pill that flexes between them, 12px above the inset.
- Home is disabled on the home grid. Tapping it shows the home grid and records "home" as the phone's shown state.
- The pill shows the shown window's app icon and title, or the avatar and "Chat" for the pinned chat window; on the home grid it shows the workspace name. It carries a count of open windows excluding the pinned chat, and shows none when that is zero. Tapping it opens the windows sheet; long-pressing it opens the shown window's menu.
- Plus opens the start sheet.

### The home grid

- Every registered non-internal app, read-only, in the order the launcher ranks apps, on the first desktop's wallpaper. No badges, no add, remove or reorder.
- Tap: if the app has a window on the client's active desktop, show the one nearest the top of this client's stack there; else if it has a window on any desktop, show the newest; else run the app's default shortcut launch. A stopped app draws faint as on desktop.
- Long-press: a small menu of the app's launch rows, each opening a new window, so a second terminal is one gesture away.

### The windows sheet

- Rows are icon and title only. Order: the windows this phone has shown, most recent first; then every other window, newest first. The pinned chat appears once, as the window on the client's active desktop, and has no X.
- Tapping a row shows that window. Each row has an X that closes the window (`409` for the pinned window never arises since it has no X) and a kebab that opens the window menu.
- "Close all" closes every non-pinned window one by one after a confirm naming the count. A search field appears when more than six windows are open and filters rows by title and app name.
- Sheets dismiss by dragging down or tapping the scrim.

### The start sheet

- The launcher's rows, tappable: launch rows first with their real labels ("New Chat", "File Viewer", "Browser", "Terminal", and the synthesized "Open Getting Started"), then window rows while typing, then the free-text rows at the foot. The field uses the launcher's placeholder, "Open an app or send a message", and is not focused on open.
- Enter runs the highlighted row with the launcher's default (the first launch or window match; New Chat when the text is a message). "Send to chat..." and "Draft into chat" are tappable rows rather than key chords. A text with a newline is a message, as today. Disabled rows keep their reasons ("Too long to send from here").
- After a send the phone switches to the chat window showing that chat. No toast.

### The window menu

- From the pill's long-press or a row's kebab: Refresh, Share <app> (only when embedded, so never on a phone), Quit <app> (stoppable apps). No Minimize, no Move or Resize, no pop-out, no Close (the X is Close).
- Quit closes the app's windows as it does on the desktop, so a phone showing one goes to the home grid; a pinned window, which is never closed, stays on the app's stopped placeholder. After the shown window is closed, by this phone or any other client, the phone goes to the home grid.

### Showing windows, landing, and switching

- Showing a window is a per-client act: the phone records it through a new client route and mounts the page. It never restores, raises or writes a placement, so laptop clients see nothing move.
- On load the phone lands on the last thing it recorded: a window still open, "home", or when nothing is recorded, the pinned chat window. Deep links (`?open=`, `?launch=`) open as today, on the first desktop, and are then shown.
- The phone switches to a window that an action of its own opened (a tap, a launch row, Getting Started's `shell:start-with-text`, a chat's `shell:open` of a sub-agent view) and to the window an agent op shows or opens when the op targets this client. It ignores `shell:focused` from pages and ops targeting other clients.
- When the window it shows is removed from the desktops, it goes home. When the pinned chat window is shown, the chat app's own per-client selection decides which chat is visible, as today.

### Opening windows from the phone

- Every open and launch the phone performs targets the first desktop with `minimized: true`, so the window exists for everyone, this phone shows it, and every other client reads it as minimized at the bottom of its stack. The phone never switches its own active desktop by opening.
- The client's active desktop still exists as a hidden variable and is only ever what the shell assigns on arrival. It decides which of the pinned chat windows and which of an app's windows the phone prefers; nothing on the phone edits it.

### Mounting, background, and resync

- Only the shown window's page is mounted, plus the pinned chat window's page, which stays mounted while another window is shown. Switching to any other window creates its page afresh; the browser app's stream stops while hidden; a terminal session is unaffected, since the terminal collects sessions by windows existing in the shell, not by frames.
- On return from the background (`visibilitychange` to visible), the shell refetches the inventory; if the socket had dropped while hidden, it also reloads the shown page.

### Banners, toasts, and installing

- The update staleness and update notice banners render above the page host as they do above the desktop, with the same buttons. The replaced-desktop notice is not shown on the phone.
- Toasts are a shell component on both layouts. Every `alert()` the shell shows today becomes a toast; `confirm()` dialogs stay. On the phone a toast sits above the bar.
- The page carries `viewport-fit=cover`, sizes itself with `100dvh`, respects `env(safe-area-inset-*)`, sets `theme-color`, titles itself with the workspace name, and serves a PNG touch icon rendered from the selected avatar design plus a web manifest, so the home-screen tile is the workspace rather than a screenshot. iOS 26 opens any saved site standalone; the top scroll-edge blur it draws over the status bar cannot be disabled from the page and is accepted.

### The chat app

- The chat app switches layout by its own width under 700px, as it does today, everywhere: on a phone and in a narrow desktop window. In that layout the rail is a drawer over the transcript, opened by a list button in a 44px header that shows the chat's title and a kebab. There is no monogram strip and no collapse toggle.
- Drawer rows are the rail's rows unchanged: status dot and title, helpers nested under their lead, stopped rows faded in place with the pause mark, unread as the green check and bold green title. Each row gains a kebab offering Rename, Stop chat or Restart chat, Delete chat, the same rows the right-click menu offers; the header kebab offers them for the chat being viewed. "New chat" is a plus in the drawer header.
- With no chat selected the drawer is open over an empty transcript that says to pick or start a chat.
- The composer keeps its placeholders, attachments, queue and stop behavior. In the phone layout the model chip moves from the under-bar to a settings button at the left of the textbox; its menu opens above the composer with the real rows (Provider with its account rows, Model, Reasoning effort, Fast Mode, Stop agent) plus a Source view row, submenus sliding on one track instead of flying out, and effort as a segmented control instead of a slider. The switch dialog and every sign-in flow keep their content and copy.
- Composer drafts already persist per chat in the browser and are restored on reload, which is what makes single mounting safe for an unsent message.

### The file viewer

- Under 700px of its own width the dufs frontend swaps the Name / Last Modified / Size / Actions table for one row per entry: the dufs octicon, the name, and "<mtime> · <size>" beneath it. Folders navigate; files open the editor page. A row kebab opens a bottom sheet with Download, Edit or View, Move & Rename, Delete, calling the same functions the table's action cells call and using dufs's own prompts.
- The header shows an up button, the folder name, a search toggle and a kebab holding the toolbox verbs: Show or Hide system files, Download folder as .zip, Upload files or folders, New folder, New file. Beneath it a scrollable breadcrumb strip; while searching, the dufs search bar replaces it and results show their relative path. Sort keys sit above the list. The editor page shows a Save button and the file's kebab.
- Everything stays a client-side patch to the vendored copy, in its own asset files, so a dufs bump re-applies two include lines.

### The terminal

- Under 700px on the wrapper page's shorter side, with a coarse pointer, a tap anywhere on the terminal focuses it and raises the keyboard, and a key strip above the keyboard offers Esc, Tab, Ctrl (one-shot: the next key is sent with it), and the arrows.
- The soft keyboard opening or closing produces a resize the ttyd frame sees, so its grid refits; the page uses `100dvh`.

### The browser

- The stream's canvas is sized to the window on the phone (`100dvh`, safe areas), and an orientation change sends a fresh size. Touch input mapping and a tab-close affordance are a follow-up.

### What agents see

- No client kind is reported. `layout.py context` and the inventory show the phone as an ordinary client; its `shown` list is the active desktop's non-minimized windows as today, and the client record additionally carries `shown_history`. Layout verbs targeting a phone act on shared placements as before.

## Implementation plan

### Shell backend (`system/apps/system_interface/imbue/system_interface/`)

- `shell/data_types.py`: `ClientRecord` gains `shown_history: tuple[str, ...]` (window ids or the literal `"home"`, most recent last, at most 20). `WindowOpenRequest` gains `minimized: bool = False`, as `LaunchRequest` has.
- `shell/clients.py`: `_StoredClient.shown_history` (default empty; a version-2 file without it reads as empty, no version bump). `ClientStore.record_shown(client_id, window_id, now)` appends the window, or `"home"` for None, and trims; `client_wire_json` adds `shown_history`. `ClientStore.drop_windows(window_ids)` prunes closed windows from every history, called from the close paths in `shell/state.py` alongside the layout-file drop.
- `shell/routes.py`: `POST /api/clients/<client_id>/shown` with `{"window_id": "<id>" | null}` (null records `"home"`); `200` the client record; `404` for a window no desktop holds. Loopback is not required: the phone calls it.
- `shell/desktop_routes.py`: the windows route reads `minimized` from the body into the `WindowOpenRequest` that `ShellState.open_window` places from; the launch route already does. The inventory adds `"workspace_name"` and each client's `shown_history`.
- `config.py`: `SYSTEM_INTERFACE_WORKSPACE_NAME` (see Decisions for its source); `server.py` `_index` writes it into `<title>` and `apple-mobile-web-app-title`, adds `theme-color`, `viewport-fit=cover`, a `<link rel="apple-touch-icon">` and `<link rel="manifest">`.
- `avatar/routes.py`: `GET /api/avatars/<id>/icon.png?size=180` rendering the design's SVG to PNG (library per Decisions); `GET /apple-touch-icon.png` and `GET /manifest.webmanifest` on `server.py` resolving the selected design.
- Tests beside each module (`clients_test.py`, `routes_test.py`, which holds the desktop routes' tests too): history bounded and pruned on close; `404` for an unknown window; `minimized` on the windows route leaves other clients' layouts without a placement; inventory fields present.

### Shell frontend (`system/apps/system_interface/frontend/src/`)

- `theme/metrics.ts`: `PHONE_MEDIA_QUERY = "(max-width: 700px), (max-height: 700px)"`, `PHONE_ATTRIBUTE = "data-phone"`, `RenderModes.isPhone` replacing `isCompact`; `COMPACT_*` removed. `theme/default.css`: phone tokens (`--desk-phone-bar-height`, `--desk-phone-control-width`, `--desk-phone-control-height`, `--desk-phone-pill-height`, `--desk-phone-sheet-radius`, `--desk-toast-*`); every `[data-compact]` rule deleted or rewritten under `[data-phone]`.
- `model/records.ts`: `ClientRecord.shown_history`, `Inventory.workspace_name`, parsers. `model/api.ts`: `recordShown(clientId, windowId | null)`, `openWindow` and `launch` carry `isMinimized`, sent as `minimized`.
- `reducers/desktopState.ts`: `DesktopState` gains `phone: PhoneState` (`shown: {kind: "home"} | {kind: "window", windowId} | null`, `history`, `sheet: "windows" | "start" | null`; which menu is open is the phone layout view's own) and `workspaceName`; events for show, sheet, history load, and the workspace name, with window removal followed by the store. Pure selectors in a new `reducers/phone.ts`: `phoneLanding(state)`, `windowsSheetRows(state)` (recency then newest), `homeGridApps(state)` (launcher order via `model/launch.ts` `orderAppLaunches`), `focusTargetOf(state, app)` (active-desktop stack top via `geometry/stack.ts`, else newest anywhere), `pinnedChatWindowOf(state)` (the active desktop's), `phonePillOf(state)`, `openWindowCount(state)`.
- `store/DesktopStore.ts`: `showOnPhone(target)` (dispatch, `api.recordShown`, page policy), `goHome()`, `openWindowAt` and `launchAt` opening on a phone at the first desktop with `minimized: true` and then showing the window, `closeAllWindows()` (sequential closes), `runHomeTile(app)`, `handleLayoutOp` mapping a `show`, `open` or `focus` aimed at this client to `showOnPhone`, `desktops_updated` handling that goes home when the shown window is gone, `onVisibilityChange(isVisible)`. `StoreDependencies.notify` goes: the store owns a `toasts` queue (`model/Toasts.ts`), and every `alert(` in the store and `index.ts` becomes a toast.
- `pages/livePages.ts`: `LivePagesLayer.setMountPolicy({kind: "all"} | {kind: "shown", windowId, alsoKeep: [pinnedId]})`; hidden pages under the shown policy are destroyed rather than `display: none`.
- New `views/phone/`: `PhoneLayout.ts` (banners, page host, bar, sheets, menus, toasts), `PhoneBar.ts`, `HomeGrid.ts` (the first desktop's wallpaper, and each app's `appGlyph`), `WindowsSheet.ts`, `StartSheet.ts` (reuses `reducers/launcherRows.ts` and the `LauncherMenu` row rendering, with tap handlers for the text rows), `Sheet.ts` (scrim, grab handle, drag-to-dismiss on its own pointer handlers), `longPress.ts` (a tap that can be held); the window menu is `phoneWindowMenuRows` in `WindowMenu.ts` (Refresh, Share, Quit), and a tile's long-press menu of launch rows is `PhoneLayout.ts`'s. New shared `views/Toast.ts` and a `toasts` field on the store.
- `views/App.ts`: at the root, `state.modes.isPhone ? m(PhoneLayout, ...) : <desktop tree>`; `SoloView` unchanged. Remove the compact branches: drag and pop-out gating (`App.ts` around lines 393 and 565), title-bar control hiding (`TitleBar.ts` 148 to 152), launcher collapse (`LauncherField.ts` 86 to 98), floating-entry forcing (`desktopState.ts` around 495), `renderedState`'s maximize override.
- `index.html` (frontend): `viewport-fit=cover`, `100dvh` on the root.
- `contracts.md` section 12 gains the phone selectors: `data-phone-bar`, `data-phone-home`, `data-phone-pill`, `data-phone-new`, `data-phone-sheet="windows" | "start"`, `data-phone-window-row="<id>"`, `data-phone-window-close="<id>"`, `data-phone-window-menu="<id>"`, `data-phone-close-all`, `data-phone-app="<app>"`, `data-phone-page-host`, `data-toast`.

### Chat app (`system/apps/chat/frontend/src/`)

- `root/index.ts`: in compact, render `ChatHeader` and a `ChatDrawer` over the slot instead of `isListOnly` and the collapsed rail; the drawer opens itself when `selectedChatId === null`, over the slot's pick-or-start note.
- New `root/ChatHeader.ts` (list button, title, kebab using `rowMenuRows` for the selected row) and `root/ChatDrawer.ts` (scrim, slide, drag-left dismiss, hosts `ChatRail` with `isCompact` meaning "drawer" rather than "collapsed"). `root/ChatRail.ts`: the collapse toggle and `w-13` state go; rows gain a kebab (`data-chat-row-menu`) in compact that opens `rowMenuRows` through `createMenu`.
- `views/ChatPanel.ts`: in compact, the under-bar's `ModelProviderMenu` moves to a leading composer button (`data-composer-settings`) and `TerminalViewToggle` becomes a row in the menu; `ConnectingIndicator` and `FastModeNotice` stay under the composer. `views/ModelProviderMenu.ts` and `modelProviderMenuStyles.ts`: an anchored-above placement, a two-pane sliding track for submenus, and a segmented effort control, all under the compact flag; desktop rendering unchanged.
- `MessageInput.ts` drafts: no change; covered by a test.
- `system/apps/chat/imbue/chat/test_e2e.py`: narrow-viewport cases.

### File viewer (`system/apps/files/assets/`)

- New `phone.css` and `phone.js`, referenced from `index.html` with `?v=minds-7` (all three existing URLs bumped too). `phone.js` runs after `ready()`: under `(max-width: 700px)` it hides `.paths-table` and `.head`, renders the header, breadcrumb strip, sort keys, rows and sheets from `DATA.paths` and `PARAMS`, honoring `isShowingHiddenFiles()`, and calls `movePath`, `deletePath`, `createFolder`, `createFile` and the download URLs the table uses. The editor page gets the Save button (`saveChange`) and kebab. Marked `minds patch` as the others are; `README.md` lists it as the fourth patch.
- `system/apps/files/test_phone_layout.py` (marked `browser`): serves the assets over a static server with a fixture `__INDEX_DATA__`, asserts rows under 700px, the kebab sheet's actions reach the dufs handlers (stubbed `fetch`), sort and search.

### Terminal (`system/apps/terminal/src/terminal_app/pages.py`, `pty_page.py`, `dispatch.py`)

- The wrapper page template gains a `#keys` strip shown under 700px on the shorter side with a coarse pointer; keys post `{type: "terminal:key", key, ctrl}` and Ctrl `{type: "terminal:ctrl", armed}`. The pty origin's page gets a script (`terminal_app/pty_page.py`, added to the ttyd client as it is installed; see Decisions) that listens, feeds xterm, and focuses it from a tap in its own frame. The wrapper uses `100dvh` and re-nudges the frame height on `visualViewport` resize so ttyd refits.
- `dispatch_test.py` for the script's insertion into the installed client; `test_phone_keys.py`, a `browser` test, for the strip posting keys into a recording frame and the script feeding them to xterm.

### Browser (`system/apps/browser/src/browser/assets/index.html`)

- `#app` height `100dvh`, `viewport-fit=cover`; the resize path sends a new size on `orientationchange`. No other change.

### Docs, mock, changelogs

- `plan-desktop-interface.md`: section 4.12 becomes a pointer to this plan (touch mode text kept); section 4.11's placeholder corrected to "Open an app or send a message"; section 4.10 notes that compact mode is gone. `contracts.md`: sections 4.3, 5.3, 5.5, 11, 12 as above. `system/apps/system_interface/README.md`, chat, files and terminal READMEs updated.
- The mock moves from `/tmp/simock` to `docs/system/blueprint/desktop-interface/phone-mock/` (`mock.html`, `assets/`; screenshots excluded).
- Changelog entries for `system_interface`, `chat`, `files`, `terminal`, `browser`.

## Implementation phases

1. **Backend state and routes.** Shown history on the client record and its route, `minimized` on the windows route, inventory fields, the workspace name setting. Desktop unaffected; tests green.
2. **Phone layout skeleton.** The shorter-side mode, `PhoneLayout` with page host (shown plus pinned mounting), bar, home grid, windows sheet and start sheet over the existing store; landing and deep links; the window menu; toasts replacing `alert()` on both layouts; the compact-only branches deleted. The two existing phone e2e scenarios rewritten. A phone can now do everything the desktop could in compact mode, minus minimize.
3. **Switching and resilience.** Agent-op and own-action switching, go-home on removal, visibility resync, close-all with confirm, drag-to-dismiss, long-press menus.
4. **Install polish.** Title, touch icon, manifest, theme color, safe areas, `dvh`.
5. **Chat app phone layout.** Header, drawer, row and header kebabs, composer settings button and menu variant, empty state; chat e2e.
6. **File viewer rows.** The two asset files and their test.
7. **Terminal and browser.** Key strip, tap-to-raise, refit nudge; browser sizing.
8. **Docs and mock.** Plan and contracts amendments, READMEs, mock checked in, changelogs.

## Testing strategy

- **Frontend unit (vitest)**: `reducers/phone.test.ts` for landing choice (window, home, none), windows-sheet ordering (recency then newest, pinned once), focus target (active-desktop stack top, else newest), pill label and count; `metrics.test.ts` for the shorter-side query; `livePages.test.ts` for the mount policy destroying hidden pages except the pinned one; `DesktopStore.test.ts` for `showOnPhone` posting the route, go-home on removal, and `notify` producing a toast rather than an alert.
- **Backend (pytest)**: shown-history bound, pruning on close, `404` for an unknown window; `minimized` opens leave other clients unplaced; inventory carries `workspace_name` and `shown_history`; the icon and manifest routes.
- **Shell e2e (Playwright, Chromium, 393x852 and 852x393)**: both sizes render the phone layout; landing on the pinned chat; a laptop-opened window appears in the sheet and is minimized nowhere but shown on the phone without changing the laptop's placements; opening from the start sheet lands on the first desktop and reads minimized for the laptop client; the home grid focuses an existing window and launches when none; the sheet orders by what was shown; X closes; Close all confirms and closes every non-pinned window; the pill's long-press offers Refresh and Quit; Enter in the start sheet runs the top match and New Chat for a message; a window closed by the laptop sends the phone home; a failed open shows a toast. The existing two compact tests are rewritten to these expectations and their compact-chrome assertions dropped.
- **Chat e2e (narrow viewport)**: drawer open and close, header kebab rename, row kebab stop, composer settings button opens the menu with the effort segments and Source view, draft restored after reload.
- **Files browser test**: as listed above. **Terminal**: the client-install unit tests plus the key-strip browser test.
- **Device checks (not automated)**: iOS Safari standalone and Android Chrome: safe areas, keyboard and bar, rotation, return from background, home-screen tile.
- Both browser suites keep their coverage floors; new browser tests carry the `browser` mark.

## Decisions

The questions this plan left open, as they were settled:

- **Workspace name source.** The first of: the `SYSTEM_INTERFACE_WORKSPACE_NAME` setting; the `workspace_display_name` label minds puts on the workspace's services agent (whose environment supervisord, and so the shell, inherits), read afresh on each request so a rename in minds shows on the next load; the host's name in mngr's host record; "Workspace".
- **Touch icon rasterization.** `resvg-py`: the image has neither rasterizer, and resvg ships self-contained wheels for every platform the workspace and a laptop run, where cairosvg needs the system's cairo.
- **ttyd page customization.** ttyd already serves the patched client the terminal installs (`-I`), so the listener is a script the terminal adds to that client as it installs it. The same script focuses xterm from a tap in its own frame, the one focus iOS raises the keyboard for.
- **Focus target across desktops.** As read above: the active desktop's stack top, else the app's newest window anywhere.
- **Recording "home".** Through the same route with `window_id: null`.
- **Agent ops a phone follows.** The shell announces every targeted `show`, placed `open`, and `focus` to the target client as a `layout_op` naming the window (contracts.md section 6), which is how a phone learns what an agent put on its screen.
- **The terminal's key strip** shows on the same shorter-side rule as the shell's layout, so a phone on its side keeps it, and only with a coarse pointer: the rule reads the window's own page, and a laptop's terminal windows are mostly under 700px tall.
- **Follow-ups deliberately deferred**: browser-history integration for the back gesture; touch input and tab close for the browser app; agent notifications on phone browsers; a phone kind visible to agents; WebKit e2e; the avatar chooser and desktop settings on the phone; per-window status in the sheet and pill.
