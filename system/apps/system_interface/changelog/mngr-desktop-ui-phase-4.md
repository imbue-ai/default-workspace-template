Phase 4 of the desktop interface (`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`): the shell backend's desktop model, served beside the tabbed shell's routes, which the current frontend keeps using.

- New state: `desktops.json` (desktops with their shortcuts and shared windows; a fresh workspace gets one desktop, `Home`, seeded from every registered app's `default_shortcut`), `placements/<desktop>/<client>.json` (each client's frames, states, minimized flags, and stacking order), and an `active_desktop` on each client record beside the tabbed shell's `active_view`.

- New routes: desktops (create, settings, wallpaper, delete, shortcuts), windows (open with `if_present`, close, location reports), placements (read, save with conflict detection), and wallpapers (bundled and file). The inventory and client documents carry the desktop fields beside the old ones, and every `app` object lists its `launch_paths`.

- The socket sends `desktops_updated`, `placements_updated`, and `active_desktop_changed`, and accepts a `client_state` report naming a desktop.

- The op route speaks the desktop verbs (`desktops`, `list`, `load --desktop`, `open`, `focus`, `minimize`, `restore`, `maximize`, `place`, `close`, `navigate`, `refresh`, the shortcut and wallpaper edits) beside the address verbs, told apart by their arguments; the requester may be an address or `{app, marker}`.

- The pure editor `shell/desktop_document.py` holds every verb and geometry rule, and passes the shared geometry vectors that the desktop frontend will run too.
