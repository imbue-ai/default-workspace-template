# Phase 8: the layout file is the truth, cross-client broadcasts, the inventory endpoint, deep links, and `layout.py`

Contracts: [contracts.md](contracts.md) sections 6 to 9, 12, and 13.

## Decision taken before landing it

The phase as first written kept phase 7's model for agent ops: `layout.py` broadcast an op, a browser that had the view active applied it to its dock and autosaved, and the shell only relayed.
That needed a connected browser on the right view (the `412`), an advisory mutex, a wait-stable poll in the script, and left two writers of the layout files with two paths (the browser's saves and the shell's own pruning and rebinding).
On 2026-09-05 the user chose the other model: the client's layout file is the truth, the shell applies agent ops to it directly and broadcasts, and the owning client's windows fetch and apply.
A window can apply a pushed document without reloading any page because every page lives in the live-surface layer keyed by its address and a dockview panel is only a slot, so re-mounting the grid tears down slots, not pages (the mechanism a view switch already relies on).
Only the four verbs with nothing to store (`maximize`, `restore`, `refresh`, the interface reload) still travel to the browser as messages.

## Order of work

1. `save_id` and `base_updated_at` on every browser save, the stale-save `409`, the equal-content no-op, and the `layout_updated` broadcast after every write of a client layout, with the frontend's echo suppression (own save ids skipped; a window records the dock's serialization of a layout it applied so the autosave sees no change; an update is applied only when the fetched `updated_at` differs from the one held). Seeds are rewritten only by browser saves; a prune or a rebind edits the seed files directly.
2. The document editor (`shell/dockview_document.py`) and the op route on top of it: `open`, `focus`, `split`, `close`, `move` applied to the target client's file; creates through the relay inside the op; filing and the referenced-instance cleanup; the transient four as targeted `layout_op` messages; the mutex, the `412` for "no client on view", and every "all clients" fallback deleted.
3. `active_view_changed` and the client record as the source of the active view: `GET /api/clients`, the frontend reads its own record on boot and stops keeping the view in local storage, `load` and an op's `--view` write the record, `load_layout` and its carve-out deleted.
4. `GET /api/inventory` (`build_inventory_document`), `layout.py`'s `list`, `views`, and Everything's `shortcuts` reading it.
5. Deep links, applied by the browser locally.
6. The browser app's `url` param on `new`, and `layout.py`'s `--client`, `--action`, `--param`, and bare-URL `open`.
7. Docs, the README's shell section, the manage-layout skill, `reveal_system_interface.py` probing `/api/health`, and one changelog paragraph per touched project.

## Files

Backend (`system/apps/system_interface/imbue/system_interface/shell/`):

- `dockview_document.py` (new): pure functions over a `LayoutRecord`'s dockview JSON and tab records: find a panel by address, add a panel into a group or beside one in a direction (tree-based neighbours, sizes taken as a ratio of the anchor's own extent, a nominal 1200 by 800 root for a never-arranged view), remove a panel, focus a panel, move a panel, and the launcher-panel rules.
- `layouts.py`: `write_client_layout` (the shell's own writes, no seed), `save_browser_layout` (a browser's save: the client file and the seed, the stale check, the equal-content no-op), `materialize_client_layout`, seed-level strip and rebind.
- `layout_ops.py`: the op tables become `DOCUMENT_OPS`, `TRANSIENT_OPS`, and the read ops; the mutex is gone; the op-application functions live here over the editor.
- `clients.py`: `set_active_view`; `record_report` reports whether the view changed; `client_wire_json` carries `is_connected`.
- `inventory.py`: `build_inventory_document`.
- `routes.py`: `GET /api/inventory`, `GET /api/clients`, the stale-save `409`, and the op dispatch of contracts section 12 (client and view resolution, the document ops, the transient broadcasts).
- `state.py`: the one write path for client layouts with its `layout_updated` broadcast; pruning and rebinding go through it.
- `primitives.py`: `mint_save_id`.
- `ws_broadcaster.py`: `broadcast_layout_updated`, `broadcast_active_view_changed`, `broadcast_to_client`; `broadcast_load_layout` deleted; `broadcast_layout_op` takes the target client.
- `server.py`: the `client_state` handler broadcasts `active_view_changed` only when the stored view changed.

Frontend (`frontend/src/`):

- `models/Layouts.ts`: `mintSaveId`, the save carries `save_id` and `base_updated_at`, a `409` surfaces as `StaleLayoutSaveError`.
- `models/Inventory.ts`: `layout_updated` and `active_view_changed`; `load_layout` gone; `layout_op` narrowed to the transient verbs; `fetchOwnClientRecord`.
- `models/ClientIdentity.ts`: the active view is module state only; nothing in local storage but the client id.
- `views/DockviewWorkspace.ts`: the five document-op handlers and the geometric neighbour search are deleted; a `layout_updated` for this client's mounted view refetches and applies when the stamp differs, deferred while a tab drag or a title edit is in progress; a stale save refetches and applies; the initial view comes from the client record; deep links on load.

Browser app (`system/apps/browser`): `app.toml` declares the `url` param; `instances.py` validates it as an `AbsoluteHttpUrl`; `interfaces.py`, `bridged_fleet.py`, `session.py`, `runner.py`, `fleet.py`, and `mock_fleet_test.py` carry the start URL through to the launch and into the manifest entry written at registration.

Scripts and skills: `system/scripts/layout.py` (stdlib only; the surface of contracts section 12; no polling), `layout_test.py` and `conftest.py`; `.agents/skills/manage-layout/SKILL.md`; `.agents/skills/update-system-interface/scripts/reveal_system_interface.py` probes `/api/health`.

## Behaviour

- Two windows of one client mirror each other: window A saves with its save id, the shell broadcasts, window B applies, window A ignores its own id, and neither saves again for it.
- Two browsers (two clients) on one workspace arrange independently and share projects; a new client of a device kind starts from the most recently saved layout of that kind.
- An agent's `open` lands in the target client's layout whether or not a browser is connected; a connected window shows it within a redraw, and a browser that connects later loads it.
- A user gesture and an agent op inside the same autosave window collide once: the browser's save is refused as stale, it refetches, and that one gesture is lost while the op stands.
- `tab_rebound` still re-keys the live page in the owning window (so the terminal frame is not reloaded), and the rewrite it reports is followed by the file's `layout_updated` like every other write.
- A deep link `/?view=<id>&open=<address>` switches the requesting client and docks the instance; a stale target is ignored.
- `layout.py open https://example.com` creates a browser instance at that URL inside the op: one `POST /_instances` with `{"action": "new", "params": {"url": "https://example.com"}}` through the relay, docked like any other instance.

## Tests

- Backend: the editor over every op and placement (unit tests beside it, including a launcher-only document and a never-arranged one), save-id echo suppression, the stale-save refusal and the equal-content no-op, seed handling, the active-view broadcast firing only on change, client and view resolution of the op route, the inventory document snapshot.
- Browser: `new` with `url` starts the browser on that page (over the fake fleet, and the manifest entry carries it), an invalid or non-absolute `url` is a `400`, and `new` without it keeps opening the home page.
- `layout_test.py`: every subcommand's argument parsing, `--client`, `--action`, `--param`, the bare-URL form, the old spellings refused, output shapes.
- Frontend: `layout_updated`, `active_view_changed`, deep-link parsing, the save id and the stale error.
- e2e (kept lean; two tests): two windows of one client mirror a server-made split; a deep link lands.
- The rest of the verification is deferred to the end of the arc, per the user's direction.

## Manual verification (deferred to the arc's end)

Two browsers and two windows on one workspace as above; `layout.py` end to end from a chat with no browser connected, then with one: `list`, `open app:files --action new --param path=/data`, `split`, `move`, `rename`, `replace-url`, `delete`, `views`, `context`.

## Changelog entries

`system/apps/system_interface/changelog/mngr-better-chat-app-arc.md`, `system/changelog/mngr-better-chat-app-arc.md`, `system/apps/browser/changelog/mngr-better-chat-app-arc.md`, `.agents/changelog/mngr-better-chat-app-arc.md`.

## Exit criteria

Every op of `layout.py` lands with no browser connected and shows within a redraw on a connected one; the static checks and the targeted tests pass; every skill that names a tab does so with an address.
