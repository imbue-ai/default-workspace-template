# Minds iframe migration: one web context, cross-domain workspace embedding

## Overview

- Collapse the minds Electron app's multi-`WebContentsView` architecture (chrome / content / modal overlay per window) into a single web context: a plain `BrowserWindow` whose page is the minds chrome, embedding workspace content in a cross-origin `<iframe>`.
- The recent forwarding redesign (origin-per-service, no path rewriting, no service workers, TLS + real origins on shares) was done precisely to enable this; this spec finishes the job locally.
- The same chrome page runs identically in Electron and in a plain browser against a local `minds run`. Plain-browser mode becomes a first-class, e2e-tested deliverable and is the proving ground for a future hosted web version.
- The migration must be genuinely cross-site-correct (cookies, `frame-ancestors`, postMessage discipline), because the future web version will embed workspaces from a different domain. Nothing in this spec deploys a remote web endpoint, but every decision moves toward making that possible.
- All chrome ↔ workspace messaging is reified into a single, versioned, auditable embed contract library, with ratchet tests forbidding raw `postMessage` usage outside it in both repos.
- Rollout is a hard cutover (no compatibility flag); the old view choreography is deleted in the same PR series. Sequencing is substrate-first so the risky Electron cutover rides on a proven iframe substrate.
- Work spans two repos: the mngr monorepo (`libs/mngr_forward`, `apps/minds`) and default-workspace-template (`system/apps/system_interface`), as two coordinated PR stacks that each land green independently.

## Expected behavior

### Electron app (after cutover)

- Each window is one `BrowserWindow` loading the minds backend's chrome page; workspace content renders in a sandboxed cross-origin iframe inside that page. Visually: identical titlebar, accent, rounded content card, tooltips, modals.
- Modals (inbox, help, sign-in, accounts, settings, workspace options panel, create-inspiration modal), the workspace switcher menu, stop-mind confirmation, and the workspace context menu are all in-DOM overlays inside the chrome page. Native surfaces remain only for the quit-time shutdown prompt and the file picker.
- No warm-iframe LRU, no parking, no cross-window workspace locking: a window shows whatever it shows; two windows may show the same workspace; leaving and returning to a workspace reloads it.
- Multi-window: notifications focus the most-recently-focused window showing workspace X, else navigate the most-recently-focused window (never auto-open a window); sidebar clicks always navigate the clicking window; "open in new window" always opens a new one.
- External navigation: iframe main-frame navigations leaving the workspace family / minds origins are cancelled by the main process and opened in the system browser. `target=_blank` continues to open externally.
- Workspace renderer crash: no in-app detection in either mode. Electron 40 exposes no OOPIF process-gone signal (the app-level `render-process-gone` relays only the primary main frame's death), so a crashed workspace iframe shows Chromium's sad frame until the user re-enters or reloads; the window-level crash strip still covers a dead chrome renderer.
- Startup / error / quitting takeovers still use `shell.html`; deeplinks, session restore, and the quit flow keep their current semantics (session restore records one chrome URL per window; old `window-state.json` files are still accepted).
- The orphaned `persist:workspace-content` partition is left on disk; workspace sessions self-heal via preauth + the `/goto/` bridge.

### Plain-browser mode (vanilla Chrome against local `minds run`)

- The browser signs in to minds via the printed login URL, exactly as today.
- Entering a workspace routes the iframe through the new auth bridge: minds `/forward-bridge?next=/goto/<host-id>/` → forward `_bridge` (opaque token check, sets the bare-origin session cookie) → `/goto/` (sets the workspace-family cookie) → workspace. No OTP is consumed; the flow is invisible to the user.
- Workspaces load and function inside the iframe: chat, terminal (incl. clipboard), apps, permission-request cards opening the inbox modal, the Claude sign-in mint modal (the "minds chrome present" ack), recovery redirect on stuck workspaces.
- TLS is trusted via a mkcert-style local CA managed by the forward plugin; after a one-time `trust` install there are no interstitials. Local browser use is a testing/dev surface, so the OS trust-store write is acceptable.
- Chromium is the supported target; Firefox/Safari are best-effort (issues noted, not blocking).
- Browser mode has no window management, no native dialogs, no crash detection (user reloads the tab); `bring-app-to-front` is a documented no-op.

### Standalone `mngr forward`

- Plain-HTTP mode keeps working with `SameSite=Lax` cookies; embedding is unsupported there.
- TLS mode: session cookies become `SameSite=None; Secure; Partitioned`; the proxy emits `frame-ancestors 'self'` + the workspace-family wildcard by default (deny external embedding), extended by `--embedder-origin` values. This is a breaking change for anyone iframing workspace origins today: changelog + README note, no deprecation window.

### Security model (both modes, and the future web version)

- The fronting proxy owns `frame-ancestors` (narrowly-blessed carve-out: the proxy may add response headers, never touch bodies or existing headers). Enforcement of that header is what makes "being framed at all" proof the embedder is allowed.
- Given that, the contract library needs only structural checks: workspace side accepts messages only from `event.source === window.parent`; parent side only from `event.source === contentFrame.contentWindow` plus an `event.origin` check against the workspace coordinate it navigated to. `targetOrigin: '*'` is acceptable and used.
- The workspace iframe carries `sandbox` (standard allowances minus `allow-top-navigation`) and an `allow` permissions-policy delegating clipboard and fullscreen. Exact attribute set is asserted by e2e (terminal copy/paste, external links, downloads).

### Back-compat with old-template workspaces

- Old workspaces (updated via `update-self` at their own pace) remain functional under the new chrome: permission requests already post to `window.parent`; close-active-tab is verified against an old-template workspace; the Claude sign-in modal degrades to its existing "desktop app required" fallback text until the workspace updates.

## Implementation plan

### 1. `libs/mngr_forward` — the substrate

- `server.py`
  - Both `set_cookie` sites (`_handle_subdomain_auth_bridge`, the bare-origin authenticate): when `use_http2` (TLS) is true, emit `SameSite=None; Secure; Partitioned`; keep `Lax` on the plain-HTTP path. Starlette's `set_cookie` has no `partitioned` kwarg in all versions — append the attribute to the `Set-Cookie` header manually if needed.
  - New `GET /_bridge?token=<opaque>&next=<path>` route on the bare origin: constant-time compare against the spawn-passed browser-bridge token; on match set the bare-origin session cookie (same attributes as above) and 302 to the sanitized `next` (reuse `_sanitize_next_url`); 403 otherwise. No token → 404 when the flag was never passed.
  - Response-header injection: on proxied responses (HTML and non-HTML alike — the header is harmless on assets and needed on documents), append `Content-Security-Policy: frame-ancestors 'self' <family-wildcard> <embedder-origins...>`. The family wildcard is `https://*.<host-coordinate>:<port>` for the workspace's own family (the shell embeds its service origins). Never modify existing headers (multiple CSP headers compose by intersection).
- `cli.py` / `config.py`
  - New flags: `--browser-bridge-token <value>` (env `MNGR_FORWARD_BROWSER_BRIDGE_TOKEN`), `--embedder-origin <origin>` (repeatable; minds passes both `http://localhost:<port>` and `http://127.0.0.1:<port>` forms).
  - New `trust` concern for the local CA (see below) — either a `mngr forward trust` invocation path or a dedicated flag; pick whichever fits the existing single-command click structure with least distortion (see Open questions).
- `tls.py` — local CA
  - Replace the per-startup self-signed cert with: a persistent CA (key + cert) at `$MNGR_HOST_DIR/plugin/forward/ca/`, generated once with the `cryptography` package; per-startup leaf certs for `localhost`, `*.localhost`, `127.0.0.1` minted from that CA.
  - Trust installation (user-consented, mkcert-style): macOS `security add-trusted-cert` into the login keychain; Linux system store (`update-ca-certificates`) plus NSS db (`certutil -d sql:$HOME/.pki/nssdb`) for Chrome. Idempotent; prints what it does; uninstall documented.
  - Electron continues to trust programmatically (no CA install needed for the app itself).
- `primitives.py` / `data_types.py`: `BrowserBridgeToken`, `EmbedderOrigin` types per house style.
- Tests: `server_test.py` (cookie attributes per mode, `_bridge` accept/reject/absent, header injection incl. composition with an existing CSP), `tls_test.py` (CA persistence, leaf minting), `cli_test.py` (new flags).
- `README.md`: document embedding support, the carve-out, the breaking default-deny; `changelog/mngr-convert-to-iframe.md`.

### 2. `apps/minds` — embed contract library

- New `apps/minds/embed_contract/` (chrome side owns the contract):
  - `embed-contract.js`: dependency-free browser JS, loadable both as a minds static asset and as a vendored import in the workspace's vite build. Exposes:
    - `createEmbedderEndpoint({ frame, isExpectedOrigin, handlers, debug })` — parent side; internal `event.source === frame.contentWindow` + `event.origin` checks; `send(type, payload)` posts into the frame.
    - `createWorkspaceEndpoint({ handlers, debug })` — workspace side; internal `event.source === window.parent` check; `send(type, payload)` posts to the parent with `targetOrigin: '*'`.
    - Message types (v1, existing inventory only): workspace→parent `open-request-modal`, `open-help`, `open-ai-keys-page`, `bring-app-to-front`; parent→workspace `close-active-tab`, `open-ai-keys-ack`. Per-type payload validators (the id-shape regexes currently in `content-relay-preload.js` move here).
    - Tolerance policy baked in: unknown types ignored; existing types immutable; a `CONTRACT_VERSION` constant lives in the module and doc, not on the wire.
    - Debug hook: when enabled (`MINDS_DEBUG_EMBED=1` surfaced as a data attribute / localStorage flag), log every sent/received message (type + origin, no payloads).
  - `embed-contract.md`: the auditable contract doc — message inventory, payload schemas, the three security invariants (proxy `frame-ancestors`, structural source checks, parent-side origin check), tolerance policy, version history.
- Minds serves the module (e.g. under `/_static/embed_contract.js`); the workspace consumes it from the vendored-mngr tree (`system/vendor/mngr/apps/minds/embed_contract/embed-contract.js`) via a vite alias.
- Ratchet in `apps/minds/imbue/minds/test_ratchets.py`: allowlist-by-file (not count-based) — `postMessage(` / `addEventListener("message"` outside `embed_contract/` fails immediately for any new file; existing violations enumerated by path and driven out as call sites migrate.

### 3. `apps/minds` — backend + chrome unification

- `cli/run.py`: generate the browser-bridge token alongside the preauth cookie; pass `--browser-bridge-token` and both `--embedder-origin` forms to `mngr forward`; expose the token to the app state.
- `app.py`: new `GET /forward-bridge?next=<path>` (requires `minds_session`): 302 to `<forward-origin>/_bridge?token=...&next=...`. Validates `next` is a forward-origin path shape (`/goto/...`).
- `chrome.js` + `pages/Chrome.jinja` + `ChromeShell.jinja`:
  - The iframe becomes the content surface in both modes; Electron-specific hiding of the iframe is removed. Workspace entry builds `iframe.src` as the minds `/forward-bridge` URL (browser) or direct (Electron, which pre-sets cookies — it can also just use the bridge uniformly; prefer uniform).
  - Iframe attributes: `sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals"` (no `allow-top-navigation`) and `allow="clipboard-read; clipboard-write; fullscreen"` — final set validated by e2e.
  - Titlebar/accent/breadcrumb state derives from parent navigation intents + the existing SSE (`workspaces`, `system_interface_status`, requests); the cross-origin URL poll, `content-url-changed`, `current-workspace-changed`, and `accent-changed` IPC paths are deleted.
  - All workspace-directed messaging goes through the embedder endpoint (permission modal opening, close-active-tab, ai-keys ack).
- Overlay unification: port `overlay.js`'s modal-host behavior into the chrome page as an in-DOM overlay layer (same-origin modal iframes for `/inbox`, `/help`, sign-in, accounts, settings, `ws-options`, sidebar menu, tooltips). Both modes use it; the anchor math simplifies to plain DOM measurement (no more rect-packing through URLs).
- Delete browser-mode full-page fallback *navigation paths*: the `/workspace/<id>/options` full-page twin and its swappable-path entries, inbox/help-in-frame navigation branches in `chrome.js`. Routes that back overlay modals stay.
- DOM replacements for stop-mind confirmation and the workspace context menu (both modes); Claude sign-in mint ack is sent whenever a minds chrome is present (browser included) and the mint modal opens in the overlay.
- Docs: rewrite the view-architecture sections of `docs/desktop-app.md`, `desktop_client/README.md`, `docs/overview.md`; update `minds-dev-workflow` skill references; `changelog/mngr-convert-to-iframe.md`.

### 4. `apps/minds/electron` — the collapse

- `main.js` (major rewrite, mostly deletion):
  - `BrowserWindow` per window (frameless / `hiddenInset` options unchanged); window navigates between `shell.html` (loading/error/quitting) and the backend chrome URL.
  - Delete: content/modal `WebContentsView`s, `view-layout.js`, `content-relay-preload.js`, `chrome-crashed.html` plumbing, partition cookie-sync, overlay bounds/replay IPC, navigation-claim + parked-residency + workspace-uniqueness machinery, the white-mirror coordination, `parseWorkspaceId`-driven surface routing (`surface-routing.js` shrinks to what the accent/titlebar still needs, likely nothing — prefer deleting it and its tests).
  - Cert trust (`setCertificateVerifyProc`, loopback-only) and the preauth + bridge cookies move to the default session; preauth cookie set with `sameSite: 'no_restriction'`, `secure: true`.
  - External-navigation enforcement: intercept subframe main-frame navigations (`will-frame-navigate` on the window's webContents, or `webRequest` fallback if the pinned Electron lacks it) leaving the workspace family / minds origins → cancel + `shell.openExternal`.
  - Crash detection: `render-process-gone` / `webFrameMain` for the iframe's process → IPC to the chrome page → swap iframe to the reload UI.
  - Multi-window rules per Expected behavior; session restore stores the chrome URL per window and accepts the old format.
- `preload.js` slims to native-only affordances: window controls, `showFilePicker`, quit/status/error channels for `shell.html`, `bringAppToFront`, deeplink-driven navigation, crash-reload IPC, debug flags. Everything content- or modal-related is deleted.
- Keyboard shortcuts: `Cmd+W` keeps its semantics (workspace displayed → `close-active-tab` via the contract; else close window) driven from `before-input-event`.

### 5. default-workspace-template — contract consumption

- `system/apps/system_interface/frontend`:
  - Vite alias to the vendored contract module; a single `embedEndpoint` instance (workspace side).
  - `permission-card.ts`, `ClaudeLoginModal.ts` (post to parent, not own window), `DockviewWorkspace.ts` (close-active-tab handler) migrate to the endpoint.
- Ratchet in the system_interface ratchet suite: allowlist-by-file for raw `postMessage` / message listeners outside the endpoint module.
- Verify old-template compat (close-active-tab against an old workspace) and record the compat matrix in the template changelog.
- `changelog/` entry in the template repo; separate PR stack based on `mngr/test-forwarding`-lineage branches.

## Implementation phases

- **Phase 1 — substrate (mngr_forward + minds bridge)**: cookies (`None; Secure; Partitioned` on TLS), `_bridge` endpoint + `--browser-bridge-token`, `--embedder-origin` + frame-ancestors injection with default-deny, local CA + trust install, minds `/forward-bridge` route + spawn plumbing. Proof: a vanilla-Chrome browser session loads a workspace inside the chrome iframe end-to-end; first browser-mode e2e (auth bridge → workspace load) lands. Electron untouched and still green (its cookie injection is compatible with the new attributes).
- **Phase 2 — embed contract**: `embed_contract/` module + doc, minds-side ratchet, chrome page migrates its message handling onto the embedder endpoint; template-side PR consumes the module (workspace endpoint + ratchet). Proof: permission-request postMessage e2e round-trip in browser mode; old-template compat verified.
- **Phase 3 — chrome unification**: in-DOM overlay layer for both modes, browser fallback navigation paths deleted, DOM stop-confirm/context menu, sandbox/allow attributes, Claude mint ack in browser mode. Proof: browser-mode e2e covers modal system + mint ack + recovery redirect; Electron still on old architecture but its overlay now backed by the same DOM layer where shareable.
- **Phase 4 — Electron collapse (hard cutover)**: `BrowserWindow` rewrite, deletions, native enforcement (external nav, crash), session/cert moves, multi-window simplification, slimmed preload. Proof: migrated Electron e2e suite green; manual pass at implementer discretion.
- **Phase 5 — e2e completion + cleanup**: full browser suite (frame-ancestors assertions, switching, sandbox behaviors) joins the Modal snapshot CI stage; delete remaining dead code (`surface-routing.js` if fully dead, relay/crash pages, view tests); docs rewrite; changelogs finalized; ratchet counts trimmed.

## Testing strategy

- **Unit (Python, mngr_forward)**: cookie attribute matrix (TLS vs plain HTTP), `_bridge` token compare / missing-flag 404 / bad `next` sanitization, frame-ancestors header construction (default-deny, embedder origins, family wildcard, coexistence with an app's own CSP), CA persistence + leaf minting.
- **Unit (Python, minds)**: `/forward-bridge` auth + redirect construction; template rendering of iframe attributes; deletion assertions where useful (e.g. no `_embed`-era leftovers).
- **Unit (JS)**: contract module in `apps/minds/test/unit/` (node): source/origin gating, payload validators, unknown-type tolerance, debug hook; system_interface side in its vitest suite via the vendored import.
- **Ratchets (both repos)**: allowlist-by-file for `postMessage(` and message-listener registration outside the contract folders.
- **e2e — browser mode (new, plain Chromium, joins the Modal snapshot stage)**: auth bridge → workspace load; workspace switching; permission-request postMessage → inbox modal; overlay modal system (sidebar, settings); Claude sign-in mint ack; recovery-redirect on stuck workspace; frame-ancestors header assertions; sandbox behaviors (terminal clipboard, external link opens new tab, download works).
- **e2e — Electron (migrated)**: existing specs (`local-swap`, `recovery-redirect`, `landing-stopped-mind-restart`, `macos-launch`) re-targeted from view-pages to the single page + iframe (frame locators over CDP); plus external-nav enforcement and crash-reload specs.
- **Back-compat check**: scripted verification of close-active-tab and permission-card against a workspace pinned to the pre-contract template.
- **Edge cases**: `_bridge` replay with wrong token; iframe navigation to another workspace's origin (allowed — family rules are per-coordinate, parent origin check updates on navigation intents only); plain-HTTP standalone forward (no embedding, Lax cookies still work top-level); two windows on the same workspace (both live, no interference beyond duplicate connections); quit flow with a modal open; session restore from an old-format `window-state.json`.
- **Manual verification**: left to implementer judgment per phase (house rule of exercising the feature as a real user still applies); the e2e suites are the merge gate.

## Open questions

- **Starlette/hypercorn `Partitioned` support**: confirm the pinned versions emit the attribute (else the manual header-append path in `server.py` is the plan of record).
- **CA trust-install mechanics**: exact per-platform commands and consent UX (macOS keychain prompts; Linux NSS for Chrome); whether to shell out to `mkcert` when present instead of reimplementing — decide during Phase 1 with a bias toward the smallest maintained implementation.
- **`trust` invocation shape**: `mngr forward` is a single click command today; decide between a `--trust` flag, a sibling command, or a minds-driven prompt that invokes the plugin's machinery.
- **Electron `will-frame-navigate` availability**: verify against the pinned Electron version; fall back to `webRequest` main-frame-subframe filtering if absent.
- **Uniform vs Electron-direct workspace entry**: recommend Electron also routes through `/forward-bridge` for one code path; confirm no startup-latency objection (one extra local 302 per workspace entry).
- **Vendored vite import ergonomics**: the alias into `system/vendor/mngr/apps/minds/embed_contract/` must survive the template's build isolation; validate early in Phase 2.
- **`bring-app-to-front` retention**: it is Electron-meaningful only; decide whether it stays in contract v1 (as a documented browser no-op) or moves to a native-only channel since the login page runs in the iframe either way.
- **Release sequencing across repos**: the minds release that ships the new chrome should reference a template release whose vendored mngr contains the contract module; confirm the release-minds flow ordering (template vendor sync happens at minds release time, which satisfies this, but the compat matrix covers the interim).
