Phases 6 and 7 of the desktop interface (`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`): the tabbed shell is deleted and the desktop is the only model the shell serves.

- Gone from the backend: the instance relay and every `/_instances` route, `/api/tabs/<id>/instance`, `/api/apps/<name>/changed`, the projects and layouts stores and their routes, the per-device layout seeds, the migration marker, and the `dockview_document` editor. The inventory now reads the registry and probes liveness only; it fetches no instance lists and takes no nudge.

- The op route speaks the desktop's verbs alone (`context`, `desktops`, `list`, `load`, `open`, `focus`, `minimize`, `restore`, `maximize`, `place`, `close`, `navigate`, `refresh`, `reload_system_interface`, `shortcuts`, `shortcut_set`, `shortcut_move`, `shortcut_remove`, `wallpaper`); a requester is `{app, marker}` or null, and an op naming an address or a view is refused. `context`, `desktops`, and `list` answer from the inventory document (`{desktops, apps, clients}`).

- The client record loses `device_kind`, `active_view`, and `previous_view`; `clients.json` is at version 2, and a version 1 file is folded into it on first read. The client-activity report is keyed on the client's desktop (`desktop_id`) rather than the view, and view-switch events are no longer logged.

- The shell's WebSocket carries `apps_updated`, `desktops_updated`, `placements_updated`, `active_desktop_changed`, and `layout_op` only; `projects_updated`, `layout_updated`, `active_view_changed`, and `tab_rebound` are gone.

- Ratchets: the retired-address ratchet now also refuses `app:` literals in the shell package, its frontend, and the shared library, and a new ratchet refuses the tabbed shell's `dockview` and `dock` vocabulary in the same sources (with a `CLEANUP:` note to relax it once the vocabulary is long gone). The "shell names no app" rule is reworded for a shell with no addresses.

- The README describes the desktop model, the routes and socket the process actually serves, the state files, and the launcher; the tabbed shell's sections are gone.
