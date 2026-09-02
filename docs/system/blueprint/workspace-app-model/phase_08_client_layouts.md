# Phase 8: client-scoped layouts, the tab route, the inventory endpoint, deep links, and `layout.py`

Contracts: [contracts.md](contracts.md) sections 5 to 9, 12, and 13.

## Goal

Finish the cross-client machinery: client-tagged layout broadcasts with save ids, per-client active view pushed across a client's windows, the tab route the terminal app uses, client pruning, the inventory endpoint, deep links, and the rewrite of `layout.py` and every skill that speaks refs.

## Files

Modified, backend (`shell/`):

- `routes.py`: adds `POST /api/tabs/<tab_id>/instance`, `GET /api/inventory`, `GET /api/clients`.
- `layouts.py`: every save broadcasts `layout_updated` with the save id; seeds are rewritten on every save; a save whose `updated_at` is older than the stored one is refused with `409` so a stale window cannot clobber a newer arrangement.
- `clients.py`: `active_view_changed` broadcast; the 90-day pruning sweep at start and daily.
- `layout_ops.py`: `load` sets the client's active view server-side and broadcasts; ops without `--client` resolve the requester through `client_activity.context` as today.
- `inventory.py`: `build_inventory_document()`.

Modified, frontend:

- `models/Inventory.ts`: handles `layout_updated` (apply when own client and foreign save id), `active_view_changed` (switch when own client), `tab_rebound` (re-address the tab, add to the view's tab set, save).
- `views/DockviewWorkspace.ts`: deep-link handling on load (contracts section 13), stripped from the URL with `history.replaceState`.
- `models/ClientIdentity.ts`: active view no longer in local storage; the server's client record is the source on connect.

Rewritten:

- `system/scripts/layout.py`: the surface of contracts section 12; stays stdlib-only; reads the registry for app names and origins as today; `open <app>` resolves the action; `rename`, `delete`, `replace-url` call the relay routes; `list`, `views`, `context` read `/api/inventory` and the client-activity summary; the wait-stable predicate reads the requesting client's layout.
- `system/scripts/layout_test.py`.
- Skills: `manage-layout`, `manage-projects`, `build-app` (the surfacing step and the beacon one-liner in `scaffold_flask_lib.py`), `update-app`, `update-system-interface` (`reveal_system_interface.py` opens `app:si-preview` and probes `/api/health`), `launch-task` (`layout.py open app:chat?instance=$MNGR_AGENT_ID`), the caretaker and automation prompts in `.mngr/settings.toml`, `.agents/shared/references/service-processes.md`, and `.agents/skills/update-self/scripts/update_self.py surface-chat-tab` (address form).

## Behaviour

- Two windows of one client mirror each other: window A saves with its save id, the shell broadcasts, window B applies, window A ignores its own id.
- Two browsers (two clients) on one workspace arrange independently and share projects; a new client of a device kind starts from the most recently saved layout of that kind.
- `tab_rebound` from the terminal app re-points the tab that switched or was renamed, exactly as today's `terminal_session` message did, and adds the new address to the view's tab set.
- A deep link `/?view=<id>&open=<address>` switches the requesting client and docks the instance; a stale target is ignored.
- `layout.py open https://example.com` creates a browser instance at that URL through the relay.

## Tests

- Backend: save-id echo suppression, stale-save refusal, seed rewrite, active-view broadcast, tab route validation (unknown tab, app mismatch), pruning removes files and records, the inventory document snapshot.
- `layout_test.py`: every subcommand's argument parsing, the old spellings refused with the new form named, `open` resolution order, `--client` and `--view` handling, output shapes (inline snapshots).
- Frontend: `layout_updated` and `active_view_changed` and `tab_rebound` handling, deep-link parsing.
- e2e: two contexts of one client mirror; two clients diverge; closing the last file-browser tab in every client deletes the instance; a deep link lands; the terminal switch test from phase 3's manual list becomes an e2e test over a real tmux (marked `tmux`).

## Manual verification

Two browsers and two windows on one workspace as above; `layout.py` end to end from a chat: `list`, `open app:files --action new --param path=/data`, `rename`, `replace-url`, `delete`, `views`, `context`.

## Changelog entries

`system/apps/system_interface/changelog/mngr-better-chat-app-arc.md`, `system/changelog/mngr-better-chat-app-arc.md`, `.agents/changelog/mngr-better-chat-app-arc.md`.

## Exit criteria

Every test passes and every skill that names a tab does so with an address.
