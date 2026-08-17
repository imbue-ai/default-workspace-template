# Plan: Mithril SPA migration of the minds desktop client

## Overview

- Rewrite the desktop client's UI from server-rendered JinjaX + ~7.5k lines of vanilla JS to a client-rendered Mithril SPA, in one pass on this branch (`mngr/mithril-refactor`), deleting legacy as each replacement lands. Merged as a single PR.
- Replace the `/_chrome/events` SSE stream (and the Electron main process's centralized-SSE + IPC fan-out) with **one WebSocket per window**, owned by the renderer. WS does not count against the browser's 6-per-host HTTP/1.1 cap, which is the root fix for the parallel-request pressure.
- flask-sock/simple_websocket runs under cheroot via the spiked gateway adapter (verified 2026-08-02; see `~/specs/mithril-migration.md` addendum and `~/specs/mithril-migration-flask-sock-cheroot-spike.py`). The adapter is ~30 lines: inject a `dup()`'d socket fd into the WSGI environ for Upgrade requests, and suppress cheroot's response-write + keep-alive for hijacked requests. No server swap; cheroot's keep-alive semantics (load-bearing for Electron auth startup) are untouched.
- Server-side push inverts from per-connection derive loops to **one edge-driven chrome-state publisher** fanning finished payloads through a ported `WebSocketBroadcaster` (inner-app pattern: bounded per-client queues, eviction, sentinel shutdown). Snapshot-on-connect replaces re-assert timers; purely edge-driven (missing change-callbacks are bugs to fix, not paper over).
- All channel messages and API bodies are explicit pydantic models with a schema version; TS types are **generated from the pydantic JSON Schema** as a frontend build step.
- Mutations stay on HTTP: client-generated operation ids make retries idempotent; resource versions + `If-Match`/412 guard state writes minds owns; mngr's host/agent locks cover concurrent access underneath.
- New session-authed internal surface at `/ui/api/*` + `/ui/ws`; `api_v1` stays agent-facing and untouched.
- The point of the rewrite is simplification, performance, and web-readiness. UX surfaces (onboarding, workspace options, backups) get simplification license — improve, don't mirror.

## Expected behavior

- App looks and behaves the same at the level a user would describe it: same hub URL paths (`/`, `/create`, `/creating/<id>`, `/settings`, `/accounts`, `/inbox`, `/recovery/<id>`, ...), same titlebar/sidebar/switcher, same workspace iframe.
- Hub navigation is instant (client-side routing); the titlebar never rebuilds; no swap-engine watchdog fallbacks.
- Exactly one long-lived connection per window (the WS) at idle, plus the workspace iframe's own traffic. Transient op-log SSE streams (create/destroy/backup/restart/discard) remain unchanged for now.
- First paint is seeded from bootstrap JSON inlined into the Flask-served index page — the full connect-time snapshot, so workspace tiles render with zero extra round trips and no accent/crumb flash.
- Login: a minimal dependency-free static HTML page (one-time-code flow unchanged). `templates/auth/` is gone. Full auth overhaul remains a separate project.
- Recovery/health: workspace health states arrive over the WS; unhealthy workspaces surface a manual Restart action and a click-in Recovery page (logs + restart). No auto-redirect to Recovery, no redirect latches, no 15s re-asserts, no auto-restart of stopped workspaces.
- Modals (accounts, account plan, settings, workspace options, signin) are in-DOM Mithril components — identical in Electron and browser mode (browser mode loses its full-page-navigation fallbacks). The overlay WebContentsView/iframe layer is gone.
- Requests inbox: the SPA decides auto-open from `requests` messages; Electron main only focuses the window over IPC. Native file picker used when the Electron bridge exists; plain text input in browser mode.
- WS drop (server restart, sleep/wake): silent backoff reconnect, full snapshot re-sync on reconnect, subtle "reconnecting" indicator after N failures. A `reload_ui` message triggers reload to pick up new hashed assets after updates.
- Onboarding/Welcome is folded into Landing/Create where possible — fewer steps, same capabilities.
- Electron main no longer consumes SSE/WS at all; renderer→main IPC forwards the few events main acts on (`workspace_stopped` closes windows; focus requests).
- In a plain browser tab the app is fully functional (modals, inbox, options) — web-readiness is real, not aspirational.

## Implementation plan

### New frontend: `apps/minds/frontend/`

- `package.json` — joins the `apps/minds` pnpm workspace; mithril 2.x, TypeScript, Vite, Tailwind v4, vitest, eslint, prettier (mirror inner-app versions; 14-day `minimumReleaseAge`).
- `vite.config.ts` — builds into `imbue/minds/desktop_client/static/ui/` with hashed filenames; `@source` config so Tailwind sees TS view files; Tailwind `@theme` token layer ported verbatim from `static/app.css`.
- `src/boot.ts` — reads inlined bootstrap JSON, seeds stores, mounts router.
- `src/router.ts` — Mithril routes mirroring today's hub paths (incl. `/_dev/styleguide`).
- `src/channel/` — WS client: json envelope, schema-version check, backoff reconnect + snapshot re-sync, `reload_ui` handling (re-implement inner-app `ws-json`/`backoff` patterns; no code sharing).
- `src/models/` — one store per domain, vitest-tested, keyed by `agent_id` with `host_id` carried for content URLs (document clone/move rationale here): `WorkspaceStore`, `HealthStore`, `RequestsStore` (incl. auto-open decision), `AccountStore`, `ProvidersStore`, `OperationTracker` (client-generated operation ids, retry semantics), `VersionedResource` helper (If-Match/412 rebase).
- `src/views/` — `Shell` (Titlebar, Sidebar, Switcher, WorkspaceFrame), one module per page, `components/` rebuilt from the JinjaX primitive catalog (Button/Card/Modal/Notice/StatusBadge/Spinner/Icon16/TextInput/Select/Textarea/TitlebarButton/...; `templates.py` constants become TS constants).
- `src/generated/` — TS types emitted from the pydantic JSON Schema (gitignored; built by the codegen step).
- `src/electron-bridge.ts` — feature-detected preload bridge: window controls, native picker, focus, event forwarding to main.
- Imports `embed_contract.js` verbatim (unchanged file, still the single postMessage confinement point; ratchets keep applying).

### Server: new `/ui` surface in `desktop_client/`

- `ui_models.py` — pydantic models for every WS message (`hello` w/ schema version, `workspaces`, `providers_state`, `requests`, `health`, `discovery_health`, `workspace_stopped`, `open_help`, `reload_ui`, client→server `client_state`) and every `/ui/api` body; resource `version` fields where minds owns the record.
- `ws_gateway.py` — productionized spike adapter: `Gateway_10` subclass (dup'd fd injection + hijack-aware `respond()` override), `wsgi.Server` subclass wiring it in; replaces the bare `wsgi.Server` in `server.py`.
- `ui_channel.py` — `WebSocketBroadcaster` port (bounded queues, consecutive-full eviction, sentinel shutdown) + the `/ui/ws` route handler (auth in `before_request` so rejection precedes hijack; ping-interval keepalive; per-connection `client_state` registration).
- `ui_publisher.py` — the chrome-state publisher: subscribes to backend-resolver / health-tracker / inbox / watchdog change callbacks, re-derives + diffs payloads once, broadcasts. Also builds the connect-time snapshot (single code path shared by WS connect and bootstrap JSON). Replaces the per-connection generator loop and `chrome_event_broadcast.py`.
- `ui_api.py` — session-authed blueprint: bootstrap/index route (Flask template inlining bootstrap JSON), JSON replacements for all SSR-embedded data and HTML fragments (inbox rows + latchkey request detail as typed JSON per request kind, destroyed-workspaces rows, create-attempt records, account plan section, settings/options data — audit every `render_*` in `templates.py` for inputs), mutation routes with idempotent operation ids + If-Match where applicable.
- `request_handler.py` — `render_request_detail_fragment` replaced by a typed-JSON detail method per request kind.
- Static login page (dependency-free HTML) served on unauthenticated routes; `/authenticate` + `/forward-bridge` flows unchanged.
- `scripts/generate_ui_schema.py` (minds) — dumps the consolidated JSON Schema for codegen; a drift test asserts generated TS is current.
- `server.py` — shutdown ordering extended: broadcaster sentinel-shutdown before `server.stop()` (spike-verified 0.5s vs 5.5s); fix the incorrect "pool grows" comment; size `numthreads` explicitly (fixed pool: windows x (1 WS + keep-alive HTTP) + transient streams).

### Deletions (as replacements land)

- `templates/` (all JinjaX incl. pages + primitives + auth), `templates.py`, `templates_auth.py`, JinjaX dependency, `warm_template_caches`.
- `static/*.js` legacy (chrome.js swap engine, sidebar, overlay_layer.js, modal_bridge.js, onboarding.js, workspace_options.js, backup UI JS, auth.js, ...); `app.css` token layer moves into the SPA stylesheet.
- `/_chrome` + `/_chrome/events` routes and the SSE generator in `app.py`; `chrome_event_broadcast.py`.
- STUCK machinery: re-assert interval, per-connection redirect latches, auto-redirect, auto-restart-on-observe; health tracker slims to state transitions only.
- Electron main: centralized SSE + IPC broadcast, `primeViewWithCachedChromeState`, chrome-state cache, swap-related channels; startup auth probe becomes a plain HTTP call to the bootstrap endpoint.

### Build & packaging

- Wheel-build hook (hatch build hook in `apps/minds/pyproject.toml`) runs `pnpm install --frozen-lockfile && pnpm build` so the bundle lands inside the `imbue` package; never committed. CI asserts the built wheel contains the bundle.
- `scripts/build.js` (Electron packaging) inherits the bundle via the wheel it already builds.
- `just` recipe for the dev loop: Vite watch-build into the served static dir alongside `just minds-start`.

### Visual-diff harness

- `apps/minds/scripts/visual_diff.py` gains an SPA capture mode: serve the built bundle, mount each route with fixture data (fixture bootstrap JSON + canned WS messages), screenshot per scenario. Ported styleguide route is its first subject; legacy capture mode kept until the final deletion commit so before/after comparisons span the migration.

### Docs & bookkeeping

- Update `desktop_client/README.md` (architecture), `templates/README.md` retired in favor of a frontend conventions doc (component catalog rules, model/view conventions, embed-contract pointer).
- Document agent_id vs host_id semantics (identity vs logical machine; clone = new both, move = new host_id, same agent_id) in the store module + glossary.
- Changelog entries for `apps/minds` (and `dev/` if root scripts change); ratchet counts trimmed as violations disappear with deleted files.

## Implementation phases

Phases 0-2 are sequential (foundation). Phase 3 tranches are delegable in parallel once the shell + channel land — each names its routes, endpoints + models, and tests as its contract. Phase 4 is sequential cleanup. The Python suite stays green at every commit (tests deleted/updated with their subjects).

- **Phase 0 — Harness + scaffold**: visual-diff SPA capture mode; `frontend/` scaffold (Vite/TS/Tailwind/vitest/eslint); token layer port; component catalog (primitives) + `/_dev/styleguide` route as its showcase. App still fully legacy-served.
- **Phase 1 — Server foundation**: `ui_models.py` + schema dump + TS codegen; `ws_gateway.py` (with spike scenarios crystallized as tests); `ui_channel.py`; `ui_publisher.py`; bootstrap/index route; wheel-build hook. Legacy SSE still running in parallel (both surfaces alive only within the branch).
- **Phase 2 — Shell**: router + Shell views (titlebar/sidebar/switcher), WorkspaceFrame + embed-contract wiring, WS client with reconnect, Electron IPC bridge + main-process slimming, static login page. Hub pages render as stubs; legacy chrome deleted here.
- **Phase 3 — Page tranches** (parallelizable, each deletes its legacy):
  - T1: Landing + Create + Creating + create-attempt records (op-log SSE consumers stay).
  - T2: Settings + Accounts + AiKeys + account/plan/signin modals.
  - T3: Workspace options panel (incl. Share tab) + WorkspaceSettings (simplification license).
  - T4: Backups (operations UI, history, tables) + Destroying + DestroyedWorkspaces + Recovery (new never-auto-navigated behavior).
  - T5: Inbox + latchkey request kinds (typed JSON details, native-picker degradation) + Help/report/assist.
  - T6: Onboarding/Welcome fold-in (simplified) + Consent + error pages.
- **Phase 4 — Final cleanup**: delete every remaining legacy file (`templates/`, `templates.py`, residual static JS, SSE routes, STUCK machinery), Electron renderer-contract Playwright spec rewrite, full-suite + acceptance green, before/after visual-diff review, README/docs/changelog, `numthreads` sizing + comment fix.

## Testing strategy

- **Frontend unit**: vitest model tests (stores, channel reconnect/backoff, operation tracker, version rebase logic) — inner-app style, no DOM needed for models; component tests where behavior warrants.
- **Python unit**: `ws_gateway` (handshake, teardown-without-EBADF, non-WS routes unaffected, auth-rejected upgrade → plain 401, cheroot `Gateway.respond` source-drift guard), `ui_publisher` (edge-driven derivation, diffing, snapshot build), `ui_channel` (eviction, sentinel shutdown), model schema snapshot (inline-snapshot of the JSON Schema — the wire contract), If-Match/412 and idempotent-operation-id semantics per mutating route.
- **Contract sync**: CI check that `src/generated/` TS is up to date with the pydantic schema (regenerate + diff).
- **Integration** (`test_*.py`): real cheroot server + real WS client end-to-end (connect, snapshot, publisher push, reconnect resync, ordered shutdown timing); bootstrap JSON matches WS snapshot; login flow against the static page.
- **Electron**: rewrite the renderer-contract Playwright specs (macos_launch CI job) against the SPA shell; e2e launch flows (`electron_full_flow_e2e.py`, `launch_to_msg_e2e.py`) re-validated.
- **Visual**: visual-diff captures per tranche (legacy capture vs SPA capture), reviewed at each tranche merge into the branch; full before/after sweep in Phase 4.
- **Manual**: full `minds-dev-workflow` pass (create → chat → options → backup → destroy), plain-browser-tab pass, sleep/wake + server-restart reconnect, multi-window.
- **Edge cases**: WS drop mid-operation (op tracker resumes via status polling), 412 conflict path (two windows editing settings), pool saturation behavior (bounded, documented `numthreads`), unauthenticated WS upgrade, stale generated types, workspace with old system_interface (embed contract v1 unchanged).

## Open questions

- Codegen tool choice (`json-schema-to-typescript` vs `quicktype`) — decide in Phase 1 by trying both on the real schema (discriminated unions matter).
- Exact resource-version storage for records minds owns (per-record `version` int in `workspace_record_store` vs content hash) — decide when wiring the first If-Match route.
- Whether the `/desktop` cookie-only namespace under `/api/v1` folds into `/ui/api` now or in the later op-log cleanup pass.
- How much of onboarding folds into Landing vs Create — design during T6 with the simplification license.
- Schema-version bump policy (single int, bump on any breaking change, client hard-reloads on mismatch?) — proposal due in Phase 1.
- Whether the WS also carries the future op-log streams (deferred cleanup pass; explicitly out of scope here).
