# Phase 7: the shell core

Contracts: [contracts.md](contracts.md) sections 1, 5 to 8, 10, 12, and 17 (deep links and the inventory endpoint land in phase 8).
This is the phase that deleted the per-kind code; it is the largest commit of the arc.
This document records what landed, and where it departs from the plan as first written.

## Goal

Make the shell generic: an app-agnostic inventory over the registry and every app's instances API, addresses everywhere, one verb definition keyed by capabilities, shortcuts and projects as data, the location relay, the New Tab page as the only empty state, and the state files of contracts section 7.

## What landed

### Backend: the `shell/` subpackage

`system/apps/system_interface/imbue/system_interface/shell/` is the whole of the shell's backend from here on; the chat modules stay at the package root until phase 10.

- `primitives.py`: `Address` (parsed and validated per contracts section 1, with `app` and `key`), `ViewId`, `ProjectId`, `ClientId`, `TabId` (`tab-<16 hex>`, minted here), `SaveId`, `DeviceKind`, `EVERYTHING_VIEW_ID`.
- `data_types.py`: `Project`, `Shortcut`, `LayoutRecord`, `TabRecord`, `ClientRecord`, `ClientStateReport`, `ClientActivityReport`, `TabInstanceReport`, `AppInventoryEntry`, `InventoryInstance`, `LayoutSaveRequest`.
- `inventory.py`: `AppInventory`, a `MutableModel` over the registry file: the watchdog registry watch (moved from the agent manager), the liveness sweep (moved from the root `liveness.py`; supervisord for rows with a `program`, a TCP connect otherwise), the per-app instance fetcher with the 250 ms nudge coalescing and the 30-second reconciliation sweep, the synthesized single-instance record, the `error` and `stopped` rewrites, the grace period for a fresh `referenced` instance, and the `apps_updated` broadcast diffed against the last one. `liveness.py` beside it holds the supervisord and TCP probes.
- `instance_relay.py`: the relay of contracts section 6 (`create`, `delete`, `rename`, `location`), passing the app's status and body through.
- `projects.py`: `projects.json` reads and writes under one state-files lock, tab sets, shortcuts (set replaces in place), create with the seed from every registered app's `default_shortcut`, settings, delete (with the fallback view), and the sweep that drops an address from every tab set.
- `layouts.py`: per-client layouts and per-device seeds under `layouts/<view>/`, `TabRecord` bookkeeping, the grid pruning helper (moved from `strip_panel_from_content`), tab lookup and rebinding for the tab route, and the referenced-address check every save runs.
- `clients.py`: `clients.json`, active view, last seen, and the 90-day pruning sweep.
- `client_activity.py`: the append-only log at `data/.state/system_interface/events/client_activity/events.jsonl` with the `message` and `view_switch` shapes, the `context` summary, and the client-for-instance attribution the layout ops use.
- `layout_ops.py`: the op tables (known, mutating, broadcasting, addressed), `list`, `inspect`, `views`, and the advisory `LayoutMutex` (a `MutableModel` with a TTL field).
- `routes.py`: module-level view functions registered by `register_shell_routes(application)` for every route of contracts sections 5 and 6 (the inventory and clients endpoints are phase 8's), plus `/api/layout/broadcast`, whose addressed ops parse the address and refuse an unregistered app before anything is broadcast.
- `state.py`: `ShellState` (inventory, the three stores, the activity log, the broadcaster, the mutex, the relay's http client) built by `build_shell_state`; `start()` prunes stale clients and starts the inventory, and `delete_unreferenced_instances()` is the referenced-deletion rule of contracts section 4.1, run after every layout save and tab-set removal.
- `testing.py` and `conftest.py`: registry rows and files, a fake fetcher and prober, `build_inventory`, and a real instances API served over loopback for the relay tests.

Modified at the package root:

- `server.py` shrank to the shell's serving of `/`, assets, the not-built placeholder, the WebSocket loop, and `register_shell_routes`. The shell page no longer injects the primary agent id or hostname meta tags (the chat page keeps them). The WebSocket loop sends `apps_updated` and `projects_updated` on connect, handles `client_state` (registering the client, recording it, and logging a `view_switch`), and, as a marked carve-out until phase 10, still sends `agents_updated` and the proto-agent messages for the chat pages.
- `main.py` takes `--state-dir` (default `data/.state/system_interface`), builds the shell state, and starts it; the shell's inventory reads the chat row's `/_instances` at the shell's own port like any other app's.
- `agent_manager.py` lost the registry watch, liveness, auto-open, the ledger, and every reference to the member stores; it keeps everything chat, records the last-messaged stamps for the OOM restart seed in `message_stamps.py` (under `data/.apps/chat/`), and nudges the shell on every agent-list broadcast.
- `chat_document.py` gained `GET /api/agents` and the `destroy`, `start`, and `stop` verbs (dispatched to the chat app by path, so loopback callers keep working), and posts every send to the shell's `POST /api/client-activity` from a thread.
- `ws_broadcaster.py` keeps the queue and eviction policy and gains `broadcast_projects_updated`, `broadcast_tab_rebound`, and `has_client_on_view`.

Deleted: `member_titles.py`, `member_last_used.py`, `member_locations.py`, `app_instances.py`, `auto_open.py`, the root `client_activity.py`, `layout_ops.py`, `projects.py`, and `liveness.py`, and their tests; every `/api/terminals*`, `/api/browsers*`, `/api/member-*`, `/api/apps/instances*`, `/api/projects/members*`, `/api/projects/panels/*`, and `/api/apps/<name>/deregister` route.

### Frontend

All under `frontend/src/`:

- `models/Inventory.ts` replaced `AgentManager.ts` for the shell: the WebSocket client, `apps_updated`, `projects_updated`, `layout_op`, `load_layout`, `tab_rebound`, the `client_state` report, the address helpers (`addressFor`, `parseAddress`, `isAddressUnlisted`), `findInstance`, `listInstances`, and `instancePageUrl` (the app's origin label on a workspace host, its registered loopback URL otherwise, with `{tab}` substituted).
- `models/Projects.ts` rewritten over tab sets and shortcuts; `models/Layouts.ts` (new) fetches and saves a client's layout with its `TabRecord` map and mints tab ids; `models/Relay.ts` (new) calls the relay routes and the lifecycle routes; `models/http.ts` is the shared JSON post.
- `views/DockviewWorkspace.ts` rewritten around one content kind: every non-launcher panel is an iframe of an address, panel ids are tab ids, `liveSurfaces.ts` keys by address, the iframe src is derived at render, the verb handlers call the relay, `apps_updated` reconciles every panel against the inventory (pruning unlisted addresses, showing the stopped placeholder for a stopped app), `tab_rebound` re-keys the surface, and the `shell:open`, `shell:focused`, and `shell:location` handlers live here. Every open in a project files the address into the project's tab set, subagent pages included. Deleting the mounted project (from any client) moves the client to the view it would land on afresh.
- `views/tabMenu.ts` replaced `objectMenu.ts`: entries from the record's `renameable`, the app's `instances`, `program`, and `critical`, plus `Share <app>` and the rail's `Remove from project` or the tab's `Close tab`.
- `views/Sidebar.ts`: shortcut rows from the project's `shortcuts` (Everything: every openable app's primary action, fixed), the tab list from the view's tab set resolved against the inventory, statuses from records, and the rail row menu from `tabMenu.ts`.
- `views/NewTabLauncher.ts`: tiles from every app's primary action, the two tables from the tab set and the inventory, `last_active` from records, an app filter per table.
- `views/AllAppsPicker.ts`: every app's primary action not yet pinned; pinning is a shortcut on the project, nothing more.
- `views/IframePanel.ts` carries `data-app` and `data-address`; `views/appIcon.ts` reads the inventory. The chat page's own `AgentManager.ts`, `AgentTerminalPanel.ts`, and `agentLiveness.ts` moved under `src/chat/`; `locationBeacon.ts`, `derived-names.ts`, `MemberTitles.ts`, `MemberLastUsed.ts`, `MemberLocations.ts`, `AppInstances.ts`, `Browsers.ts`, `appLiveness.ts`, `TerminalBanner.ts`, and `objectMenu.ts` are gone.
- The shell stops accepting the `minds-location` beacon spelling; the `build-app` scaffold posts `shell:location`.

### Other apps and scripts

- `system/scripts/layout.py` speaks addresses (the half of phase 8's rewrite that is grammar): `app:<name>` and `app:<name>?instance=<key>`, a bare word as `app:<word>`, the old spellings refused with an error naming the new form, an external URL refused with an error naming phase 8, `open` of a bare app with instances running its action through the connected client and printing the created address to stdout, `rename`, `delete`, and `replace-url` through the relay, and `shortcuts`, `shortcut set`, and `shortcut remove`. `--view` is the flag; `--layout` stays as an alias. Every caller (`update_self.py`, the caretaker, migrate-workspace, manage-scheduled-tasks, manage-layout, manage-projects, `serve_isolated_instance.py`) speaks the new form.
- The terminal app drops the `/api/terminals/notify` forward of phase 3 and defaults a new terminal's `workdir` to its own working directory.
- The browser's manager lookup raises `UnknownBrowserError` and the runner's routes catch it.
- The chat manifest drops `internal`; `program` and `priority` stay with their `CLEANUP` for phase 10.
- `test_embed_ratchets.py` drops `locationBeacon.ts` from the allowlist and gains a ratchet forbidding the retired ref prefixes as string literals anywhere under the shell package and the frontend (tests excepted, since they assert the refusals).

## Decisions taken while landing it

These were the open questions going in, with the answer each got:

1. The address grammar of `layout.py` came forward from phase 8, since every caller had to change with the backend; `--client`, `--action`, `--param`, deep links, the inventory endpoint, and `open https://` stay in phase 8.
2. `GET /api/agents` and the destroy, start, and stop verbs moved to the chat app now, dispatched by path.
3. `agents_updated` and the proto-agent messages stay on the shell's socket as a marked carve-out until phase 10.
4. The shell's chat "Stop" verb is gone; "Stop agent" lives in the chat page's model bar menu.
5. `POST /api/tabs/<tab_id>/instance` and the `tab_rebound` broadcast came forward from phase 8.
6. Every open in a project files the address into the project's tab set, subagent pages included; there is no second rule.
7. Chat sends post to the shell's `POST /api/client-activity`; the chat keeps its own last-messaged stamps under `data/.apps/chat/` for the OOM restart seed.
8. The chat manifest drops `internal`; one marked exception stays in the launcher and the dock for the chat's `new` action (the provider picker's `account_id`, the chooser when nothing is signed in), a `CLEANUP` for phase 10.
9. The terminal app defaults `workdir` to its own working directory.
10. The Playwright suite and the layout pipeline test run for real against a stub app registered beside the chat row.

Also: `load_layout` (the `load` op's message) stays until phase 8 lands `active_view_changed` and `layout_updated`; the first landing after this commit is the New Tab page, with no starter project and no auto-open, until phase 9's migration creates one.

## Behaviour

- On connect a client receives `apps_updated` and `projects_updated`, fetches its layout for the active view, and renders; a tab whose address is not in the inventory renders the stopped placeholder when its app is stopped, waits while its app's list has not arrived yet (`is_listed` false), and is pruned from the layout otherwise.
- Opening an action: `POST /api/apps/<name>/instances` through the relay, then dock the returned record's url beside the active tab, add the address to the view's tab set (projects only), and record `last_focused_ms`.
- Focus mode: the most recently focused open tab of that app in this client; else the app's most recently active instance the view lists, opened; otherwise the action runs.
- Delete: relay, then the `apps_updated` that follows the shell's refetch prunes the tab everywhere.
- Close: undock; when the app's record for that address says `referenced` and the shell holds no other reference across every project's tab set and every client's layout, the shell calls the app's delete through the relay (the check runs on every save).
- Stop and Start: for rows with a `program` that are not `critical` and do not share a critical app's program.
- Layouts are per client, with seeds; the cross-client broadcasts land in phase 8, so a second window of the same client sees changes on reload.
- The shell writes nothing under the mngr host or agent state directories; `MNGR_HOST_DIR` and `MNGR_AGENT_ID` are no longer read by any shell module.

## Tests

- `shell/*_test.py` for every module, over a real instances API served over loopback where the relay is involved.
- `test_layout_pipeline.py` over addresses: the script against a server whose registry holds the chat row and a stub app, including the retired spellings, the relay verbs, and the shortcut subcommands.
- Frontend unit tests for `Inventory.ts`, `Projects.ts`, `Layouts.ts`, `tabMenu.ts`, `Sidebar.ts`, `NewTabLauncher.ts`, `AllAppsPicker.ts`, `liveSurfaces.ts`, and the dock's pure helpers.
- `test_e2e.py` over the chat row and a stub app: the New Tab landing, opening a row (and its filing), the chat page's rendering, layout ops over addresses, projects and views, one live page per instance, rename, delete, unfiling, pinning, the launcher filter, the tab strip, drag overlays, and the mobile seed.

## Exit criteria

The shell package contains no reference to chats, terminals, browsers, or files by name outside the two marked chat carve-outs, and every verb works against the stub app and the built-ins.
