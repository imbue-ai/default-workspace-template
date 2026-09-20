Phase 5 of the desktop interface (`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`): the workspace shell's frontend is now a desktop. The tabbed shell (the dockview workspace, the project rail, the New Tab page, and the models behind them) is gone from the bundle; the frontend reads only the desktop routes and socket messages phase 4 added.

- Windows over a wallpaper: each window is a live page of an app at a path, with a title bar (the page's reported title, its app's icon, a menu with refresh, share, stop or start, and close), eight resize edges, and the contract's data attributes. Windows drag, resize, snap to a half by dragging to an edge, maximize by dragging to the top or double-clicking the title bar, un-snap by dragging away, minimize to the taskbar, and raise on a press; every arrangement is this client's own and is saved to its placement file (debounced, with the stale-save conflict handled by a refetch). Pages are held alive and hidden across minimize and desktop switches, interleaved with the chrome in one stacking order, inert unless focused, and follow their window's path in place (`shell:navigate`) or by reload.

- A backdrop with shortcuts: a grid of launch-path shortcuts (double click, or a tap on touch, runs one; the context menu opens another, flips its mode, or removes it), dragged between cells with collisions displacing the occupant, and the desktop's wallpaper (one bundled wallpaper, `dawn`, ships with the shell) or its colour.

- A taskbar with a launcher field, one entry per window (click to raise, minimize, or restore; right-click or long-press for its menu), and a system tray: a Desktops widget (one glyph per desktop to switch, a menu to create, open settings, or delete) and a Running apps widget (one icon per running app, a popover listing its windows and launch paths, and adding a shortcut).

- A launcher overlay with the apps' launch paths as tiles, the windows on this desktop (every desktop's while searching), the "Start something" intents and the template catalog (kept from the New Tab page), and a search over all of it.

- Desktop settings: name, colour, glyph, wallpaper, and sharing, with an in-place delete confirmation.

- Compact (narrow) and touch modes off media queries: every window maximized, icon-only taskbar entries, a launcher button, no resize edges, long press for menus.

- Deep links: `?desktop=<id>`, `?open=<app>:<path>`, `?launch=<app>:<launch>`, stripped from the URL once acted on. The embedder's close chord closes the focused window.

- The app contract's handshake now says which window, desktop, and path a page is in (and still the desktop under `viewId`, for the chat's client-activity report).

- The relay also trusts a page on the origin the shell itself framed, so apps on their own loopback ports (a local workspace) can speak to the shell.

- A pixel-metric ratchet keeps hard-coded `px` values out of the views and reducers; the theme tokens (`--desk-*`) carry every metric, and the frontend reads them off computed style.

- The end-to-end suite is rewritten for the desktop: stub apps serve stand-in pages that import the shell's contract module, and the scenarios cover every open path, the gestures and their persistence, two clients, URL following with and without in-page navigation, shortcut drags, desktops through the tray, the launcher and the widgets, and a phone viewport with touch.
