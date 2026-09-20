Phases 6 and 7 of the desktop interface (`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`), the skills:

- A single `manage-desktop` skill replaces `manage-layout` and `manage-projects`: the desktop's vocabulary (desktops, windows, placements, clients, launch paths, shortcuts), which client an op targets, how to name a window (`win-<hex>`, `self`, an app name), and the `layout.py` verbs for opening, arranging, closing, navigating, and editing a desktop's shortcuts and wallpaper. Every skill that cited `layout.py` (update-app, update-system-interface, build-app, agentic-browser-fleet, caretaker, crystallize-creation, heal-creation, update-creation, migrate-workspace, manage-scheduled-tasks) speaks the desktop's verbs.

- The update apply's post-restart probes poll `/api/health` on every critical app the user can open (`critical = true` and not `internal = true`; the chat and the terminal), at the URL each app's registry row names, in place of the retired instances API.

- `update-self` no longer rebuilds a files app tool (the app has no Python package) and no longer runs the layout migration on apply; `build-app` documents the manifest without the retired `instances` and `actions` keys.
