# Phase 6: chat as a document

Contracts: [contracts.md](contracts.md) sections 4.3 (chat row), 10, and 11.
After this phase every chat renders inside an iframe at the `chat` origin, served by the same system-interface process, and behaves as it did before.

## Goal

Split the chat pages and their bundle out of the shell's document and serve them at a registered `chat` origin from the existing process, with the instances API implemented over the existing agent manager, so phase 7 can make the shell generic with chat already an ordinary app.

## Files

Created:

- `system/apps/chat/app.toml` (contracts section 2, `instances_url` omitted, program `system_interface` for now; `# CLEANUP:` phase 10 sets `program = "chat"`), `system/apps/chat/icon.svg`.
- `system/apps/system_interface/imbue/system_interface/chat_document.py`: `create_chat_application(state) -> Flask`, the second Flask app; serves `GET /<agent_id>` and `GET /<agent_id>.<session_id>` (the built `chat.html` with the base-path, hostname, agent-id, and session-id meta tags), the instances blueprint at `/_instances`, and every chat route moved off the shell (below).
- `system/apps/system_interface/imbue/system_interface/chat_instances.py`: `AgentManagerInstanceSource`, the `InstanceSourceInterface` over `AgentManager`: lists non-primary agents plus the provisional and subagent records it keeps in memory, with status derived from `activity_state`, the harness's pending-permission signal, and the lifecycle state; `create_instance` for `new` mints the agent id as `create_chat_agent` does today, stores the provisional record, and starts creation on the most recently used account when one exists; `create_instance` for `subagent` returns or creates the `<parent>.<session>` record; `delete_instance` runs the existing destroy path for an agent and drops the record otherwise; `rename_instance` runs the existing rename path; the nudger fires on every observe event and every activity, model, queue, or permission change.
- `system/apps/system_interface/imbue/system_interface/wsgi_dispatch.py`: `build_dispatching_application(shell_app, chat_app, chat_label_reader)` routing a request to the chat app when the Host's first label equals the chat row's label or the path starts with `/_instances`, else to the shell.
- `system/apps/system_interface/imbue/system_interface/presence.py`: per-agent, per-client presence (`visible`, `hidden`, `closed`) with a ten-minute expiry, replacing the frontend's `/api/activity` reports as the OOM prioritizer's open and visible inputs.
- `system/apps/system_interface/frontend/chat.html` and `frontend/src/chat/index.ts`: the chat document's entry, mounting the moved chat views for the agent named by the page's meta tags, connecting to the shell through `app_contract`, and posting presence.
- `system/apps/system_interface/frontend/src/app_contract.ts`: the contract module of contracts section 10, built as its own library entry and served at `/_static/app_contract.js`.
- `system/apps/system_interface/frontend/src/relay.ts`: the embedder relay of contracts section 11, registered from `embed.ts`.

Moved into `frontend/src/chat/` (unchanged apart from imports): `views/ChatPanel.ts`, `MessageInput.ts`, `ModelBar.ts`, `FastModeModal.ts`, `ProviderChooserModal.ts`, `SubagentView.ts`, `AgentTerminalPanel.ts`, `TerminalViewToggle.ts`, `TerminalBanner.ts`, `ProgressBlock.ts`, `OutgoingMessageView.ts`, `QueuedMessageView.ts`, `ProtoAgentLogView.ts`, `TranscriptScrollbar.ts`, `ActivityIndicator.ts`, `accountRow.ts`, `chat-flip.ts`, `conversation-rows.ts`, `fast-mode-prompt.ts`, `latchkey-scope-info.ts`, `message-*.ts`, `permission-card.ts`, `providerMarks.ts`, `providerSignInStyles.ts`, `scroll-selection.ts`, `transcript-scroll-engine.ts`, `turn-grouping.ts`, `user-message-display.ts`, `modelCardStyles.ts`, and `models/Conversation.ts`, `ComposerAttachments.ts`, `FastModePrompt.ts`, `HarnessCatalog.ts`, `ModelSettings.ts`, `OutgoingMessages.ts`, `Providers.ts`, `Response.ts`, `StreamingMessage.ts`, `activityReporter.ts`, `attachments.ts`, `transcriptScroll/`, `lightbox.ts`, `llm-api.ts`, `markdown.ts`, with their tests.

Modified:

- `server.py`: the chat routes leave (`/api/agents/*` except nothing, `/api/harnesses`, `/api/activity`, `/api/uploads*`, `/api/claude-auth/*`, `/api/accounts/*`, `/api/latchkey/*`, `/api/proto-agents/<id>/logs`, `/api/agents/<id>/screen`); the shell keeps every other route; `create_application` returns the dispatcher over both apps.
- `main.py`: registers the chat row at startup by running `forward_port.py --manifest system/apps/chat/app.toml --url http://localhost:8000` before serving, so the row exists whenever the shell does.
- `agent_manager.py`: exposes the signals the source needs (a permission-pending flag per agent from the harness watcher, a change callback) and takes a nudger; `/api/activity`'s open and visible sets are replaced by `presence.py`.
- `oom_prioritizer.py`: reads open and visible from `presence.py`.
- `DockviewWorkspace.ts`: a `chat` panel renders an `IframePanel` at `<chat origin>/<agent-id>` (origin from `labelForService("chat")`), with `serviceName = "chat"`; the subagent view opens as an iframe at `/<agent-id>.<session-id>`; the terminal flip, composer, model bar, and provider chooser leave the shell document; `openSubagentTab` and `openIframeTabForAgent` become opens of chat pages.
- `App.ts`: drops the provider chooser and fast-mode modal mounts and the first-run greeting (the chat document greets).
- `embed.ts`: registers the relay; keeps `minds:close-active-tab` handling.
- `IframePanel.ts`: sends `shell:handshake` on every `load` event and `shell:shown` and `shell:hidden` from the live-surface visibility changes; hands `shell:focused` to the dock's activation; ignores `shell:location` and `shell:open` until phase 7 (logged).
- `vite.config.ts`: three entries (`index.html`, `chat.html`, the `app_contract` library).
- `test_embed_ratchets.py`: the allowlist gains `app_contract.ts` and `relay.ts`; `chat/` files may import the contract module only.
- `test_e2e.py`: gains `chat_frame(page, agent_id)` returning the frame locator for a chat, and every chat assertion goes through it.

Deleted: `frontend/src/views/EmptySlot.ts` if unused after the move; nothing else yet.

## Behaviour

- The chat document is one page per agent, loaded once per tab and kept alive by the live-surface registry exactly as service iframes are today.
- Presence: the page posts `visible` on handshake and `shell:shown`, `hidden` on `shell:hidden`, `closed` on `pagehide`, and a heartbeat every sixty seconds; the prioritizer treats an agent as open while any client has unexpired presence and visible while any client's last state is `visible`.
- The send path records the message for the client-activity log using the `clientId` and `viewId` from the handshake.
- Permission cards: the chat page's embed endpoint sends `minds:open-request-modal` to `window.parent`, the relay forwards it to the chrome, and `minds:permission-resolutions` comes back down through the relay to every child frame; each chat page keeps its existing per-request filtering.
- The shell still owns rename, destroy, stop, and start through the in-process agent manager in this phase; the chat row's instances API exists but the shell does not read it until phase 7.
- Keyboard: the close chord keeps reaching the shell from the chrome as today (`minds:close-active-tab`); the shell sends `shell:close-request` to the active frame so the page can flush pending state, then closes the tab. There is no veto.

## Tests

- `chat_instances_test.py`: status mapping per lifecycle and activity state, provisional create mints an id and starts creation on the MRU account, provisional record becomes the agent's record once observed, subagent create is idempotent, delete delegates, rename delegates, nudge on each signal.
- `wsgi_dispatch_test.py`: Host-label routing, `/_instances` routing, everything else to the shell.
- `presence_test.py`: expiry, heartbeat refresh, multi-client aggregation.
- Frontend: `app_contract.test.ts` (source and origin checks, handshake on load, every message shape), `relay.test.ts` (forwarding both ways, no payload inspection, non-`minds:` types dropped upward).
- e2e: every existing chat test passes through `chat_frame`; a new test asserts a permission card's open-request reaches a stub chrome through the relay.
- The conservation suites and `test_chat_creation_recovery_e2e.py` run against the chat application object.

## Manual verification

Open two chats side by side, send in one while the other streams, flip one to its terminal, open a subagent view, trigger a permission card and resolve it from the minds inbox, rename and delete a chat from the tab menu, reload the page, and confirm nothing differs from before.
Confirm in devtools that each chat is a frame at the chat origin and that the shell document contains no chat markup.

## Changelog entries

`system/apps/system_interface/changelog/mngr-better-chat-app-arc.md`, `system/changelog/mngr-better-chat-app-arc.md`.

## Exit criteria

Every test passes, the chat origin serves every chat, and the shell bundle no longer imports any file under `src/chat/`.
