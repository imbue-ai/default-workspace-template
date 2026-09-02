# Phase 7: the shell core

Contracts: [contracts.md](contracts.md) sections 1, 5 to 8, 10, and 13 (deep links land in phase 8).
This is the phase that deletes the per-kind code; it is the largest commit of the arc.

## Goal

Make the shell generic: an app-agnostic inventory over the registry and every app's instances API, addresses everywhere, one verb definition keyed by capabilities, shortcuts and projects as data, the location relay, the New Tab page as the only empty state, and the state files of contracts section 7.

## Files

Created, under `system/apps/system_interface/imbue/system_interface/shell/` (a new subpackage that is the whole of the shell's backend from here on; the chat modules stay at the package root until phase 10):

- `data_types.py`: `Address` (parsed and validated per contracts section 1, with `app` and `key` and a `render()`), `Project`, `Shortcut`, `LayoutRecord`, `TabRecord`, `ClientRecord`, `AppInventoryEntry`, `InventoryDocument`.
- `inventory.py`: `AppInventory` (the registry watch moved from `agent_manager._read_apps` and `_AppsFileHandler`, the liveness sweep moved from `liveness.py`, the per-app instance fetcher with the nudge coalescing and the reconciliation sweep of contracts section 5, the synthesized single-instance record, and the `apps_updated` broadcast with a diff against the last broadcast).
- `instance_relay.py`: the relay routes of contracts section 6 and the location relay used by the WebSocket's message path.
- `projects.py`: `projects.json` reads and writes, tab sets, shortcuts, project create, settings, delete, and the seed of a new project's shortcuts from every registered app's `default_shortcut`.
- `layouts.py`: layout and seed reads and writes, `TabRecord` bookkeeping, tab id minting, the grid pruning helper (moved from today's `strip_panel_from_content`) used when an address disappears from the inventory.
- `clients.py`: `clients.json`, active view, last seen, and the pruning sweep.
- `client_activity.py`: the append-only log at `data/.state/system_interface/events/client_activity/events.jsonl` with the `message` and `view_switch` shapes of contracts section 5, and the `context` summary.
- `layout_ops.py`: the `/api/layout/broadcast` op handlers over addresses (`list`, `inspect`, `where`, `context`, `views`, `load`, and the broadcast ops), the mutex unchanged.
- `routes.py`: `register_shell_routes(app, state)` for every route in contracts sections 5 and 6 except the inventory endpoint (phase 8).
- `state.py`: `ShellState` (inventory, projects, layouts, clients, broadcaster, http client, update staleness, static directory) built in `main.py`; the existing `SystemInterfaceState` keeps only the chat half and is renamed `ChatState` in phase 10.

Modified:

- `server.py`: shrinks to the shell's serving of `/`, assets, the not-built placeholder, the WebSocket loop, and a call to `register_shell_routes`; the chat application from phase 6 is unchanged.
- `main.py`: builds `ShellState` and `ChatState`; the shell's inventory reads the chat row's instances over loopback like any other app's.
- `ws_broadcaster.py`: keeps the queue and eviction policy; `client_state` handling moves to `clients.py`; the outbound message set becomes contracts section 8 minus phase 8's additions.
- `agent_manager.py`: loses the registry watch, liveness, `_maybe_auto_open` and the ledger, the app-instances allocator, and every reference to the member stores; keeps everything chat.
- `update_staleness.py`: unchanged.
- Frontend, all under `frontend/src/`:
  - `models/Inventory.ts` replaces `AgentManager.ts`: the WebSocket client, `apps_updated`, `projects_updated`, `layout_op`, the `client_state` report, `getApps()`, `getInstance(address)`, `labelForApp(name)`.
  - `models/Projects.ts` rewritten: `Project`, `Shortcut`, tab-set and shortcut calls, `chooseInitialViewId` unchanged in spirit.
  - `models/Layouts.ts` (new): `fetchLayout`, `saveLayout` with save ids, the `TabRecord` map.
  - `views/DockviewWorkspace.ts` rewritten around one content kind: every non-launcher panel is an iframe of an address; panel ids are `tab-<hex>`; `liveSurfaces.ts` keys by address; `PanelParams` becomes `{address, tabId}` plus the launcher marker, and the iframe src is derived at render from the instance's listed url, the app's origin label, and the tab id; the verb handlers call the relay routes; the location relay and `shell:open` and `shell:focused` handling live here; the reload-on-url-change rule of contracts section 10.
  - `views/tabMenu.ts` replaces `objectMenu.ts`: entries from the instance record's `lifetime`, `renameable`, and the app's `critical` and `program`, per meta spec section 6.4, plus `Share <app>` for any app (unchanged behaviour, through the existing embed message).
  - `views/Sidebar.ts`: shortcut rows from the project's `shortcuts` (Everything: every app with an action, fixed), the tab list from the view's tab set resolved against the inventory, statuses from instance records.
  - `views/NewTabLauncher.ts`: tiles from every app's actions, the two tables from the tab set and the inventory, `last_active` from records.
  - `views/appIcon.ts`, `derived-names.ts`, `agentLiveness.ts`: derived names and dots come from records; `agentLiveness.ts` is deleted.
  - `locationBeacon.ts` is deleted; its origin check moves into the contract listener in `DockviewWorkspace.ts`.
- `system/apps/terminal/src/terminal_app/hooks.py`: delete the compatibility forward from phase 3.
- `system/apps/files/assets/index.js`: the shell stops accepting `minds-location`.
- `test_ratchets.py` and `test_embed_ratchets.py`: the contract listener file joins the allowlist; a new ratchet forbids the strings `chat:`, `terminal:`, `service:`, `url:`, and `subagent:` as ref prefixes anywhere under the shell package and frontend.

Deleted:

- Backend: `member_titles.py`, `member_last_used.py`, `member_locations.py`, `app_instances.py`, `auto_open.py`, `client_activity.py` (root), `layout_ops.py` (root), `projects.py` (root), `liveness.py` (root), `hookspecs.py`, `plugins.py`, and their tests; every `/api/terminals*`, `/api/browsers*`, `/api/member-*`, `/api/apps/instances*`, `/api/projects/members*`, `/api/projects/panels/*`, `/api/apps/<name>/deregister` route.
- Frontend: `models/AgentManager.ts`, `AppInstances.ts`, `Browsers.ts`, `MemberLastUsed.ts`, `MemberLocations.ts`, `MemberTitles.ts`, `appLiveness.ts` (folded into `Inventory.ts`), `views/objectMenu.ts`, `AllAppsPicker.ts` (replaced by the same popover over apps with actions), `ProjectMembershipDialog.ts` (rewritten over tab sets), `DestroyConfirmDialog.ts` (kept, wording from the record), `terminalFocus.ts` (the focus grant is sent by the dock on activation for every iframe; the ttyd client ignores nothing, others ignore the type), and their tests.

## Behaviour

- On connect a client receives `apps_updated` and `projects_updated`, fetches its layout for the active view, and renders; a tab whose address is not in the inventory renders the stopped placeholder when its app is stopped and is pruned from the layout otherwise.
- Opening an action: `POST /api/apps/<name>/instances`, then dock the returned record's url beside the active tab, add the address to the view's tab set (projects only), and record `last_focused_ms`.
- Focus mode: the most recently focused tab of that app in this client's layout of this view; otherwise the action runs.
- Delete: relay, then wait for `apps_updated`; the tab is pruned by observation.
- Close: undock; when the app's record for that address says `referenced` and the shell holds no other reference across every project's tab set and every client's layout, the shell calls the app's delete through the relay (the server-side check runs in `layouts.py` on every save).
- Stop and Start: as today, for rows with `program` that are not `critical`.
- Layouts are per client from this phase (contracts section 7), with seeds; the cross-client broadcasts, pruning, and the tab route land in phase 8, so in this phase a second window of the same client sees changes on reload.
- The shell writes nothing under the mngr host or agent state directories; `MNGR_HOST_DIR` and `MNGR_AGENT_ID` are no longer read by any shell module.
- There is no migration yet: a workspace upgraded to this commit starts with empty shell state (phase 9 fills it); dev workspaces are recreated between these phases.

## Tests

- `shell/*_test.py` for every module: address parsing and rendering, inventory diffing and coalescing (with a fake clock), synthesized single-instance records, error and stopped rewrites, projects and shortcuts and seeding from `default_shortcut`, layout and seed reads and writes, referenced-deletion decisions across projects and clients, client records, the relay's passthrough of status and body, the client-activity log and `context`.
- `test_layout_pipeline.py` rewritten over addresses.
- Frontend unit tests for `Inventory.ts`, `Projects.ts`, `Layouts.ts`, `tabMenu.ts` (every capability combination), `Sidebar.ts`, `NewTabLauncher.ts`, and the dock's address and reload rules.
- e2e over a stub app (`app_instances.testing.run_stub_app`) registered beside the chat row: open an action, two instances of one app side by side, rename, delete, close a `referenced` instance and observe its deletion, stop and start an app, the location relay landing in the stub's record, `shell:open` docking a sibling.
- The chat e2e tests from phase 6 keep passing.

## Manual verification

Every verb on every app from both the tab menu and the rail: chat, terminal, files, browser, and a scratch user app.
Two file browsers on different folders reopen where they were after a reload.
A chat's status dot follows thinking, a permission request, and a stop.

## Changelog entries

`system/apps/system_interface/changelog/mngr-better-chat-app-arc.md`, `system/apps/terminal/changelog/mngr-better-chat-app-arc.md`, `system/apps/files/changelog/mngr-better-chat-app-arc.md`, `system/changelog/mngr-better-chat-app-arc.md`.

## Exit criteria

The shell package contains no reference to chats, terminals, browsers, or files by name outside the built-in manifests, and every verb works against the stub app and the four built-ins.
