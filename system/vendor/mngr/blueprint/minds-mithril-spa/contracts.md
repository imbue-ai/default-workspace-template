# Mithril SPA migration: build contracts

Working agreements for every agent contributing to this branch. Read the plan
(`plan-minds-mithril-spa.md`) first. These contracts exist so parallel agents
never edit the same file.

## Ground rules (all agents)

- Model/style: follow the repo `style_guide.md` and `CLAUDE.md`. No emojis. No
  asyncio. Pydantic for all validation. loguru for logging.
- Run ONLY targeted tests (`uv run pytest <specific file>::<test>` with
  `--no-cov`, or `cd apps/minds/frontend && pnpm test`). NEVER run the full
  suite, NEVER `just test-offload`, NEVER `uv sync` (already done).
- Do NOT run any git commands (no add/commit/checkout). The orchestrator
  commits.
- Do NOT edit files outside your ownership list. If you believe a shared file
  must change, write the needed change into your final report instead of
  making it.
- Do NOT create changelog entries (handled centrally).
- Ratchets: never increase counts. New python files need tests alongside.

## Directory layout

Server (all under `apps/minds/imbue/minds/desktop_client/`):

- `ui_models.py` — channel message models + `UI_SCHEMA_VERSION` + shared UI
  primitives (owned by foundation; tranches do not edit).
- `ws_gateway.py` — cheroot WS gateway adapter + server subclass.
- `ui_channel.py` — `UiChannelBroadcaster` + `/ui/ws` handler.
- `ui_publisher.py` — edge-driven state publisher + snapshot builder.
- `ui_api.py` — blueprint assembly + index/bootstrap route. Imports one
  `register_<area>_routes(blueprint)` from each per-area module (all six
  pre-stubbed; tranches fill their own module only).
- `ui_api_create.py` (T1), `ui_api_settings.py` (T2), `ui_api_options.py`
  (T3), `ui_api_lifecycle.py` (T4), `ui_api_inbox.py` (T5),
  `ui_api_onboarding.py` (T6) — routes AND their request/response pydantic
  models live in the owning module.
- `ui_login.py` — static login page route (Phase 2).

Frontend (all under `apps/minds/frontend/`):

- `src/channel/` — WS client, envelope, backoff (foundation/shell).
- `src/models/` — shared stores (`workspaces.ts`, `health.ts`, `requests.ts`,
  `accounts.ts`, `providers.ts`, `operations.ts`) owned by shell; tranches
  ADD new files (e.g. `backups.ts`) but never edit shared ones.
- `src/views/components/` — shared component catalog (Phase 0). Tranches may
  ADD generic components; never modify existing ones.
- `src/views/pages/<Page>.ts` — one module per route, pre-stubbed by shell;
  each tranche rewrites only its own page files.
- `src/views/pages/<area>/` — page-specific subcomponents per tranche.
- `src/router.ts`, `src/boot.ts`, `src/electron-bridge.ts` — shell-owned.
- `src/generated/` — gitignored TS types; refresh with `pnpm generate`.

## Channel protocol (`/ui/ws`)

- JSON text frames. Every message: `{"type": <str>, ...fields}` modeled in
  `ui_models.py`; `UI_SCHEMA_VERSION` int, bumped on any breaking change.
- Server→client types: `hello` (schema_version), `workspaces` (full list +
  destroying ids + restorable ids + remote states), `accounts` (launcher
  payload), `providers` (providers panel state), `requests` (inbox payload +
  auto_open), `health` (one workspace's system-interface health state),
  `discovery_health`, `workspace_stopped`, `open_help`, `reload_ui`.
- Client→server: `client_state` (client_id, route, workspace_agent_id or
  null).
- Connect sequence: `hello`, then one message of each snapshot type, then
  edge-driven updates. Snapshot messages are the same types as updates.
- Client on schema mismatch: hard reload once.
- Implementation note (Phase 1): the server uses simple_websocket directly
  (not flask-sock's route decorator, which hijacks before auth can run); the
  bootstrap seed is `{accent, is_mac, mngr_forward_origin}` (no separate
  `platform` field). The `accounts` launcher payload is its own message, not
  bundled into `workspaces`.

## HTTP API (`/ui/api/*`)

- Session-cookie auth (same decorator family as existing internal routes);
  never latchkey/gateway-reachable. `api_v1` is not touched.
- Responses/requests are pydantic models; plain JSON; errors as
  `{"error": <str>}` with proper status codes.
- Long-running actions: client supplies the operation id
  (`operation_id` in the request body, uuid4 hex); POST is idempotent for a
  repeated id. Status/log polling stays on the existing `/api/v1
  /workspaces/operations/...` resources for now.
- State writes on minds-owned records send `If-Match: <version>`; stale
  writes get 412 and the client rebases on the next snapshot.

## Bootstrap / index

- Flask serves the SPA index for every hub route; the page inlines
  `window.__MINDS_BOOTSTRAP__` = `{seed: {accent, is_mac,
  mngr_forward_origin, platform}, schema_version, snapshot: {<one entry per
  snapshot message type>}}` built by the same publisher code path as the WS
  snapshot.
- Vite builds with `manifest: true` into
  `imbue/minds/desktop_client/static/ui/`; the index route reads the
  manifest for hashed asset names.

## Hub routes (SPA router mirrors these exactly; verified against app.py)

Page routes (SPA-rendered): `/`, `/create`, `/create/inspiration`,
`/creating/<agent_id>`, `/settings`, `/settings/ai-keys`, `/accounts`,
`/workspaces/destroyed`, `/workspace/<agent_id>/settings`,
`/workspace/<agent_id>/options`, `/workspace/<agent_id>/backups`, `/inbox`,
`/destroying/<agent_id>`, `/agents/<agent_id>/recovery`, `/help`,
`/welcome`, `/consent`, `/_dev/styleguide`.

Former modal/fragment routes (`*/modal`, `/inbox/list`,
`/inbox/detail/<id>`, `/accounts/<user_id>/plan-view`,
`/workspaces/destroyed/rows`) become in-SPA components + `/ui/api` JSON
endpoints and their page routes are deleted by the owning tranche.
Action POST routes (`/consent`, `/help/report`, `/help/assist`,
`/welcome/skip`, `/accounts/set-default`, `/accounts/<user_id>/plan`,
`/accounts/<user_id>/trim-backups`, `/settings/ai-keys/mint`,
`/settings/permissions/revoke`, `/requests/<id>/grant|deny`) migrate to
`/ui/api` equivalents owned by the same tranche that owns their page.

## Post-Phase-2 facts (tranche agents rely on these)

- The workspace content surface is `/workspace/<agent-or-host-id>` (the
  `/_chrome` wrapper is deleted). `/ui/` is an alias of the index.
- Every hub route serves the SPA index; page stubs live one-per-file at
  `frontend/src/views/pages/<Name>Page.ts` — a tranche rewrites ONLY its own
  page files (keep the exported names; router.ts is shell-owned and already
  wired).
- Electron relay: the channel client forwards workspaces/health/
  workspace_stopped/open_help/discovery_health to main via
  `electronBridge.sendShellEvent` ('shell-event' IPC). `GET
  /ui/api/app-status` is the Electron startup probe (includes
  `needs_error_reporting_consent` for T6).
- `UiProviderEntry.status` is the uppercase `ProviderPanelStatus` enum.
- After ANY `ui_models.py` or `ui_api_*.py` model change: run `pnpm generate`
  in frontend/ and update the wire-schema inline snapshot
  (`ui_models_test.py`).
- `UiChannelBroadcaster` has `wait_for_connection_count` /
  `wait_for_client_ids` for tests — never sleep-poll.
- Tranches do NOT delete legacy templates/render_* functions/page JS or edit
  `scripts/visual_diff.py`, `templates.py`, `testing.py`, `router.ts`,
  `ui_api.py`, or another tranche's files — all legacy deletion happens in
  Phase 4. Legacy POST/action routes in app.py keep working; tranches add
  `/ui/api` equivalents in their own `ui_api_<area>.py` and the SPA calls
  those.
- Ratchet notes: no functools.partial, no cast, no getattr, no nested defs,
  no click.echo/bare print, no trailing `#` comments, no time.sleep outside
  bounded poll helpers that already exist. Run the ratchet suites only AFTER
  the orchestrator commits (they misfire on uncommitted files).

## Phase 4 status (deferred bulk deletion)

The SPA serves every hub route and the WebSocket channel carries all live
state; the app is fully functional. Completed in Phase 4: the `/_chrome`
page + `/_chrome/events` SSE endpoint and its re-assert/redirect machinery
are deleted (Phase 2b); the client-side STUCK auto-redirect/auto-restart is
gone (Recovery is click-only); the renderer-contract Playwright specs and
their harness were deleted along with the legacy shell scripts they drove
(no SPA-shell Playwright equivalents exist yet); README + changelog + docs
are in.

DEFERRED to a dedicated follow-up PR (intentionally NOT done here to keep the
green state stable): the bulk removal of the now-unused legacy SSR surface --
`templates/` (JinjaX), `templates.py`, `templates_auth.py` where safe, the
residual `static/*.js` page scripts, the legacy `render_*`/`_handle_*` page
functions in `app.py`, and the per-area auth-check duplication consolidated
back into one helper. These are dead-but-harmless once the SPA is the only
served UI; deleting ~tens of thousands of lines is a large, separate,
independently-reviewable change and belongs on its own branch.

## Verification expectations per agent

- Frontend work: `pnpm check` (tsc + eslint) and `pnpm test` green; new
  models get vitest tests.
- Server work: targeted `uv run pytest` green for every file you touched;
  new modules get `_test.py` neighbors.
- Report deviations honestly in your final report; list any follow-ups.
