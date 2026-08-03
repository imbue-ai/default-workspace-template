Adopt the minds embed contract for all chrome<->workspace messaging.

The system interface now imports the embed contract module from the vendored mngr tree (`@minds/embed-contract`, a vite alias to `system/vendor/mngr/apps/minds/imbue/minds/desktop_client/static/embed_contract.js`) and routes every postMessage exchange with the embedding minds chrome through one workspace-side endpoint (`frontend/src/embed.ts`). This replaces the ad-hoc `window.parent.postMessage` / relay-window messages that grew alongside the old Electron content-relay design; the minds chrome now embeds this UI as a cross-origin iframe in both the desktop app and plain-browser mode.

Concrete changes:

- Permission-request cards send `minds:open-request-modal` through the contract endpoint (same wire format as before, so older minds chromes keep working).

- The Claude sign-in modal's "Sign in with Imbue" handshake sends `minds:open-ai-keys-page` to the embedding chrome and waits for the contract ack; the ack now means "a minds chrome is present" (plain-browser chrome acks too), not "the desktop app is present", and the fallback alert wording changed accordingly.

- The dockview workspace now actually handles `minds:close-active-tab` (the chrome's Cmd/Ctrl+W forward): it closes the active tab. Previously the message was posted by the Electron relay but nothing listened.

- A new allowlist-by-file ratchet (`test_embed_ratchets.py`) forbids raw `postMessage` / `message`-listener usage outside the embed boundary, so the whole message surface stays auditable in `embed.ts` + the vendored contract (see minds' `docs/embed-contract.md`).
