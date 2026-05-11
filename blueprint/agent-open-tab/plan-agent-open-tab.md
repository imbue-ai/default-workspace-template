# Agent-driven UI tab open / refresh

## Overview

- The mind agent has no way today to surface a newly-built web service in the UI -- the user must click the `+` in the dockview tab bar and pick the service. This blueprint gives the primary agent a way to request a layout change directly so the just-built view appears alongside the chat without manual clicking.
- The agent surface is a single shared script under `scripts/` with three subcommands: `list` (enumerate registered services), `open` (request the UI to show a service tab), and `refresh` (reload an already-open service tab). The existing per-verb `refresh-service` HTTP endpoint stays in place; callers go through the script instead of POSTing directly.
- Transport mirrors the existing `refresh_service` channel exactly: script POSTs to a new loopback HTTP endpoint on the workspace server -> server writes a JSONL event into the agent's refresh-events file -> the minds desktop client tails the file via `mngr event --follow` -> desktop POSTs back to a loopback-only broadcast endpoint -> workspace server emits a WS message -> the dockview frontend applies the layout change. No new infrastructure, only a new event kind (`open_tab`) on the same pipeline.
- Default placement is a right-side split of the primary agent's chat tab with a 60% web view / 40% chat ratio so the just-built view dominates. If there is no chat panel in the current layout the new panel opens as an ordinary tab; if the requested service tab is already open it is focused in place rather than duplicated. Every request is honored, including re-opens after the user closes the panel.
- The capability is wired into `build-web-service`'s skill prompt with conditional guidance: "If the service has a visible UI, run the script's `open` subcommand after verification, and run `refresh` whenever you later change the service." Backend-only services skip both. Only the primary agent uses the script in v1; subagent enforcement is deferred (no server-side block).

## Expected behavior

- After the agent runs `build-web-service` end-to-end and the new service passes verification, the agent runs the script's `open` subcommand. Within a second or two the UI shows the new service as a right-side split alongside the chat, taking ~60% of the horizontal space.
- If the user closes the split and the agent later makes a code change to the same service, the agent runs `refresh`. Any iframe currently rendering that service reloads in place; if the user had re-opened the tab in the meantime they see the updated state. If the tab is closed at the time, `refresh` is a no-op (existing behavior).
- If the agent re-fires `open` for a service whose tab was closed after a previous `open`, the tab reopens (a fresh right-side split). Agent intent wins.
- If the agent fires `open` for a service that is *already* open as a tab somewhere in the layout, that tab is focused. No duplicate tab appears, the panel is not relocated, and the split ratio of any existing layout is preserved.
- If the agent fires `open` before the workspace_server has observed the new entry in `runtime/applications.toml`, the script retries briefly (registration is asynchronous via watchdog). If the service is still unknown after the retry window the script exits non-zero with a clear error.
- The script's `list` subcommand prints one service name per line (the contents of `[[applications]] name` keys in `runtime/applications.toml`). Easy for the agent to grep, capture, or feed to a follow-up command.
- The script's `refresh` subcommand and the existing `/api/refresh-service/<name>` endpoint do the same thing; behavior of existing iframes does not change.
- If a non-primary agent invokes the script, it succeeds at the script level but the backend treats the request like any other -- there is no server-side rejection in v1.
- The new `build-web-service` skill prompt continues to support the escape-hatch (wrap-existing) path identically; the conditional "visible UI" guidance applies regardless of which path produced the service.
- Fire-and-forget: no toast in the chat acknowledging the open, no user-facing setting to disable auto-opens, no telemetry surface.

## Changes

- Add a new shared script (under `scripts/`) that exposes `list`, `open <service-name>`, and `refresh <service-name>` subcommands. The script reads `runtime/applications.toml` for `list` and for the pre-flight registration check in `open`, and POSTs to loopback HTTP endpoints on the workspace_server for `open` and `refresh`.
- Add a new loopback HTTP endpoint on the workspace_server (`apps/system_interface/imbue/minds_workspace_server/server.py`) for the `open_tab` request. It writes an event line to the agent's refresh-events file (re-using the existing `events/refresh/events.jsonl` file, since the desktop client already tails it; new event `type: "open_tab"`).
- Extend `apps/system_interface/imbue/minds_workspace_server/request_writer.py` with a `write_open_tab_request(service_name)` helper that mirrors the existing `write_refresh_request`.
- Add a corresponding broadcast endpoint and `WebSocketBroadcaster.broadcast_open_tab(service_name)` method, parallel to the existing `broadcast_refresh_service`.
- Teach the minds desktop client's request-events consumer to handle `open_tab` events and POST back to the new broadcast endpoint, parallel to how it handles `refresh_service`.
- Frontend (`apps/system_interface/frontend/src/models/AgentManager.ts`): add an `open_tab` event type to the WebSocket dispatch and a registration mechanism (`addOpenTabListener`) parallel to the existing `addRefreshServiceListener`.
- Frontend (`apps/system_interface/frontend/src/views/DockviewWorkspace.ts`): register an open-tab listener that, given a `service_name`:
  - Looks up the matching application URL via `getApplications()`. If absent, log and drop.
  - If a panel for that service is already open, call `setActivePanel` on it and return.
  - Otherwise look up the primary agent's chat panel; if found, add a new iframe panel with `position: { referencePanel: chatPanelId, direction: "right" }` and adjust the split ratio so the new panel takes ~60% width.
  - If no chat panel exists, add the iframe panel without a position hint (dockview's default placement).
  - Mark the new panel's `panelParams` with `panelType: "iframe"` and `serviceName` so the existing Refresh and Share buttons render correctly.
- Update `.agents/skills/build-web-service/SKILL.md` to add a "Surface the view" step after verification. The step instructs the agent: "If the service has a visible UI (not a backend-only API), run `<script> open <name>` after verification passes, and `<script> refresh <name>` whenever you later change the service. Skip both for backend-only services." Include the script invocation pattern alongside the existing `forward_port.py` examples.
- Add unit/integration tests:
  - Backend tests in `apps/system_interface/imbue/minds_workspace_server/`: a request-writer test that exercises `write_open_tab_request` (file is created with the right shape), a server test that exercises the new request endpoint and the new broadcast endpoint (mirrors existing `refresh_service` coverage), and a broadcaster test for `broadcast_open_tab`.
  - Script test: invoke the script as a subprocess with a mocked HTTP server and verify argument parsing, the registration pre-flight + retry behavior in `open`, and that `list` reads `applications.toml` correctly.
  - Frontend unit tests for the dockview open-tab handler covering the three branches (focus existing, split alongside chat, fallback when no chat exists). Mock dockview to avoid a DOM/dockview integration dependency.
- Manual testing of the end-to-end flow is owned by the user; no acceptance/release test is added for the cross-process pipeline in v1.
