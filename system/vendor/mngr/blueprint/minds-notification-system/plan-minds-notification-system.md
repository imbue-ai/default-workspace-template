# Plan: minds notification system

Source design: the minds-options prototype (mind-sketches `prototypes/minds-options/`).
Background study: `specs/minds-notification-integration.md`.

## Overview

- Port the prototype's notification design into the minds app chrome: titlebar bell with a red unresolved-count badge, an anchored durable feed, a transient toast stack, and OS delivery.
- One OO notification system: an abstract notification entry + a delivery-channel abstraction, with the desktop_client backend as the single source of truth.
- V1 implements exactly one concrete notification kind: permission requests. No other subclasses yet; the schema stays additive (no urgency field) so future kinds bolt on.
- All rendering lives in the chrome (`apps/minds/frontend`). The chrome's toast layer floats over the workspace iframe, so cross-workspace awareness needs no system_interface changes. Workspace-to-app event production via the embed contract is a documented future seam, unused in v1.
- The feed is server-side, per-device, bounded (~50), seeded into every window via the existing `/ui/ws` snapshot, and backfilled from already-pending requests at startup.
- Unread is resolution-based, never view-based: a request counts until approved/denied (or auto-resolved when its workspace disappears). Opening the feed never clears the badge.
- Delivery is preference-driven (enabled + style cards/os/both, defaults enabled + both) and loosely coupled per runtime: backend-orchestrated native OS notifications on desktop, renderer Web Notifications in plain-browser mode, existing macOS/tkinter fallbacks kept behind the channel abstraction.
- The master toggle also gates the existing OS-only producers (agent-sent notifications, backup failures); they do not join the feed in v1.
- This overturns the documented "no requests entry in the titlebar" stance (Titlebar comment + `Titlebar.test.ts:58-67`); those artifacts are updated as part of the change.
- One branch/PR. The contract (data model, wire frames, preference schema) lands as the first commits; the backend and chrome tracks are then implemented in parallel against it.

## Expected behavior

- A bell button appears in the titlebar right cluster (left of the bug-report button). When any notification is unresolved, it carries a red count pill (99+ cap).
- The macOS dock / Linux taskbar icon shows the same unresolved count via Electron `app.setBadgeCount`; Windows gets no taskbar count (bell only).
- When an agent files a permission request:
  - A feed entry is created immediately (waiting state, snapshot of workspace name/accent + request title/summary + reason).
  - If the request's workspace is NOT the one on screen, a toast card flashes (per style preference) and/or an OS notification fires.
  - If that workspace IS on screen, the arrival is silent (the in-chat card already shows it); the feed and badge still record it.
  - No toast flashes while the feed overlay is open; opening the feed also retires any floating toasts.
  - In multi-window use, toast cards flash only in the OS-focused window; every window's bell count updates.
- The feed overlay (bell click, anchored under the bell, 360px, max 70% height):
  - One continuous list: unresolved entries first, then resolved, each newest-first; no section headers. An entry slides down into the resolved region when it resolves, fading to a grey receipt (opacity + grayscale).
  - Request rows: accent dot + "<workspace> requests access to <service-mark> <service>", clamped reason, relative timestamp with a red dot while pending; Approved / Denied chip once resolved; neutral "Closed" chip when auto-resolved (workspace destroyed or request vanished).
  - Pending rows are clickable; resolved rows are inert receipts. No clear-all, no per-row dismiss.
  - Empty state: bell glyph + "You're all caught up."
- Toast cards: 5s auto-dismiss, corner X (retires only the flash; the feed entry survives). Full prototype choreography: newest in front, up to 3 peeking behind at 4px slivers with scale/opacity falloff, "N more" overflow line, hover fans the pile open, mouse-leave re-collapses. 200ms slide/fade enter and exit. `prefers-reduced-motion` disables slide/scale (instant appear, plain list).
- Uniform click gesture: an in-app toast, a pending feed row, a native OS notification click, and a Web Notification click all jump to the asking workspace (chrome accent repaint) and auto-open the review popup via a deep-link route param. If the request already resolved, they navigate without opening the popup. In-chat cards keep their existing in-place popup behavior.
- Settings gains a "Notifications" section in the existing /settings modal: master on/off toggle + style radio (cards / os / both). Defaults: enabled + both.
  - Master off: no nudges at all anywhere (feed still records; badge still counts). It also silences the existing agent-sent and backup-failure OS notifications.
  - Style shapes only feed-backed (request) notifications; the existing OS-only producers dispatch to OS whenever the master toggle is on.
- Plain-browser mode: the "os" channel uses Web Notifications. Permission is requested only from a user gesture in Settings, plus a one-time dismissable hint near the bell (dismissal persisted server-side). While ungranted, the os channel silently no-ops.
- Native OS banners keep the platform default sound; in-app cards are silent.
- Startup: the backend backfills feed entries for already-pending requests, so the badge agrees with the Permissions tab from first paint. The feed is per-device and in-memory in the backend process (lost on app restart, then re-backfilled from still-pending requests).
- Cap eviction (50): oldest resolved entries are evicted first; unresolved entries are never evicted (feed may temporarily exceed the cap under pathological load).

## Implementation plan

### Contract (lands first)

- `apps/minds/imbue/minds/desktop_client/ui_models.py`
  - `UiNotificationEntry`: `id: str` (uuid), `kind: Literal["permission_request"]`, `created_at: str` (ISO), `is_resolved: bool`, `outcome: Literal["approved", "denied", "closed"] | None`, `title: str`, `body: str`, `request_id: str | None`, `workspace_agent_id: str | None`, `workspace_name: str`, `workspace_accent: str`, `service_name: str` (catalog service for the brand mark, "" when none). Additive-friendly: future kinds extend `kind` and reuse title/body.
  - `UiNotificationsMessage` (`type: "notifications"`): full ordered entry list. Broadcast on every feed change and included in `UiSnapshot` (small: ≤~50 entries).
  - `UiNotificationPrefs`: `is_enabled: bool`, `style: Literal["cards", "os", "both"]`, `is_os_hint_dismissed: bool` — added to the settings overview model.
- `apps/minds/frontend/scripts/generate-types.mjs` output (`src/generated/ui.ts`) regenerated from the schema; `src/channel/messages.ts` re-exports and adds `Framed<UiNotificationsMessage, "notifications">` to `UiServerMessage` + parser case.
- Constants module (`apps/minds/frontend/src/models/notificationConstants.ts`): TOAST_MS=5000, FEED_CAP=50, TOAST_EXIT_MS=200, TOAST_STACK_MAX=3, TOAST_PEEK=4, TOAST_EXPAND_GAP=8 — prototype values verbatim.

### Backend track (`apps/minds/imbue/minds/desktop_client`)

- New `notification_feed.py`: `NotificationFeed` (mutable model, lock-guarded like other stores)
  - `apply_pending_requests(requests)` — diff vs. known request ids: new pending id -> append entry (display fields snapshotted from the same inbox-card derivation `app.py` uses); disappeared id -> resolve with `closed` unless an outcome was recorded; outcome recorded (grant/deny paths) -> resolve with `approved`/`denied`.
  - `backfill(pending_requests)` — called once at startup before first snapshot.
  - Eviction on append: drop oldest resolved beyond FEED_CAP; never drop unresolved.
  - `entries()` — unresolved-first, then resolved, each newest-first (the wire order IS the display order).
  - `unresolved_count()` — for dispatch decisions and tests.
- `ui_publisher.py`: add `UiNotificationsMessage` to the diffed frame set (or push on feed-change signal, matching the edge-driven pattern); include in `build_snapshot`.
- Request lifecycle wiring: the existing requests producer edge (whatever signals `requests` frame changes in `app.py`) also feeds `NotificationFeed.apply_pending_requests`; grant/deny routes record outcomes; workspace destroy paths trigger the same reconciliation (vanished requests -> `closed`).
- `notification.py` refactor (organized, swappable channels):
  - `DeliveryChannel` protocol: `deliver(request: NotificationRequest, agent_display_name: str) -> None`; concrete `ElectronChannel`, `MacosChannel`, `TkinterChannel` wrapping the existing implementations; `NotificationDispatcher` keeps the priority chain but consults a `prefs_provider` callable first.
  - Master toggle off -> no dispatch at all (covers agent-sent + backup producers).
  - For feed-backed notifications: dispatch to OS only when style is `os`/`both`; set `url` to the deep-link (`/workspace/<agent_id>?review=<request_id>`) so the existing Electron click handler navigates there.
- `ui_api_settings.py`: read/write `UiNotificationPrefs` (If-Match guarded like `report_unexpected_errors`); endpoint to set `is_os_hint_dismissed`.
- `api_v1.py` / `agent_creator.py` / `backup_trim.py`: unchanged call sites; gating happens inside the dispatcher.

### Chrome track (`apps/minds/frontend` + `apps/minds/electron`)

- `src/models/notifications.ts`: `NotificationsStore` — applies snapshot/frames; exposes `entries`, `unresolvedCount`, `liveToastIds`; decides flash-on-arrival (diff vs. previous frame): skip when entry's workspace is the current route, when the feed overlay is open, when `!document.hasFocus()`, or when prefs say no cards; fires Web Notification when `!electronBridge.isDesktop` and style includes os and permission granted.
- `src/models/settings.ts`: notification prefs in the overview + setters.
- `src/views/components/icons.ts`: add `bell` (Lucide path, 16px, like existing copies).
- `src/views/shell/Titlebar.ts`: `#notifications-toggle` TitlebarButton with `Badge` count, `aria-expanded`, opening the feed overlay route. Remove the "deliberately no requests entry" comment; rewrite `Titlebar.test.ts:58-67` to assert presence.
- New `src/views/shell/NotificationsOverlay.ts`: anchored feed panel (Shell overlay system, sized like RequestOverlay). Rows per Expected behavior; row body shared with the Permissions tab's waiting rows (extract `src/views/components/NotificationRow.ts` if sharing is clean, else mirror markup).
- New `src/views/shell/ToastLayer.ts`: full choreography port (absolute-positioned stack items, measured heights via ResizeObserver, expandedTops math, overflow line); mounted in `Shell.ts` in one column with the Reconnecting chip (chip on top). `matchMedia("(prefers-reduced-motion: reduce)")` disables transforms.
- Review deep-link: `router.ts` / shell route handling consumes `?review=<requestId>` on the workspace route -> `shell.openRequestPopup(requestId)` if still pending (drop the param via history replace either way). Shared `reviewInWorkspace(requestId, agentId)` helper used by toast, feed row, and deep-link.
- `src/views/pages/settings/SettingsSections.ts`: Notifications section (toggle + radio; radio row triggers the Web Notification permission request gesture in browser mode).
- One-time hint: small dismissable chip near the bell in browser mode when style includes os and permission is `default`; dismiss persists via the settings endpoint.
- Dock badge: renderer sends `sendShellEvent({type: "notifications_count", count})` on count change; `electron/main.js` adds `notifications_count` to `KNOWN_SHELL_EVENT_TYPES` and calls `app.setBadgeCount(count)` (macOS/Linux; no Windows overlay).
- `electron/main.js` `handleNotification`: no change needed (click-through URL already supported); verify the `?review=` param passes its URL validation patterns.

### Docs / repo hygiene

- Changelog entry: `apps/minds/changelog/<branch>.md` (all touched code is under apps/minds).
- Update `apps/minds/imbue/minds/desktop_client/README.md` (SPA surface list) and the stale stance comments (`Titlebar.ts`, `models/requests.ts`).

## Implementation phases

1. **Contract** — `UiNotificationEntry` / `UiNotificationsMessage` / `UiNotificationPrefs` in ui_models.py, regenerated TS types, messages.ts union + parser case, constants module. App builds and runs; frames unused. (Everything after this can proceed in parallel: phases 2 and 3 are independent tracks on the same branch.)
2. **Backend track** — NotificationFeed + request-edge wiring + backfill + auto-resolve + eviction; snapshot + frame broadcast; settings endpoints; dispatcher channel refactor + preference gating + deep-link URLs. Working system: OS notifications for requests obey prefs; `/ui/ws` carries the feed; no UI yet.
3. **Chrome track** — store + bell/badge + feed overlay + toast layer + settings section + web-notification channel + hint + deep-link consumption + dock-badge relay. Working system against phase-1 frames (renders empty feed until phase 2 merges; both tracks integrate on the shared branch).
4. **Integration + polish** — suppression rules end-to-end, focused-window gating, reduced-motion, stale-click behavior, Titlebar test/comment updates, docs, changelog, manual verification pass, ratchet/type cleanup.

## Testing strategy

- Backend (pytest, desktop_client):
  - `notification_feed_test.py`: entry creation on new pending id; approve/deny resolution; vanished-request -> `closed`; startup backfill; eviction skips unresolved; ordering (unresolved-first, newest-first within groups); cap overflow with all-unresolved.
  - Publisher/snapshot: notifications frame in snapshot; frame broadcast on feed change (extend existing ui_publisher tests).
  - Settings: prefs round-trip + If-Match conflicts + hint-dismissed flag (extend ui_api_settings tests).
  - Dispatcher: master-off silences all producers; style gating for feed-backed dispatch; channel fallback order preserved; deep-link URL construction (extend notification_test.py).
- Frontend (vitest):
  - Store: frame application, unresolved count, flash decisions (on-screen workspace / overlay open / unfocused / prefs), Web Notification gating by permission state.
  - Toast layout math as pure functions (openTops, peek styles, overflow count) — no DOM timing assertions.
  - Parser: `notifications` frame accepted, unknown frames still ignored.
  - `Titlebar.test.ts`: bell + badge present, count rendering, 99+ cap.
  - Settings model: prefs load/save.
- Edge cases to cover in tests: request resolves while its toast is live (toast click then navigates without popup); reconnect re-seeds feed from snapshot without duplicate flashes; two requests same workspace; badge across multiple simulated frames.
- Manual verification (not crystallized into pytest, per repo convention for interactive behavior): toast choreography (peek, hover fan, "N more"), reduced-motion mode, OS banner + click-through on macOS, dock badge, browser-mode Web Notification + permission hint, multi-window focus gating.
- Repo gates: `just test-offload` full suite, ratchets (no new violations; update Titlebar assertions deliberately), `ty` type check, changelog presence.

## Open questions

- Exact copy: settings section wording, the one-time hint text, and the neutral chip label ("Closed" vs "No longer needed").
- Whether the feed's display fields can be derived from the existing inbox-card derivation without refactoring it (worst case: small extraction of that derivation in app.py).
- The deep-link param's interaction with the request popup's history-entry juggling (`shell-state.ts` replaces history entries when swinging the popup between requests) — resolve while implementing; fallback is a transient store handoff instead of a URL param.
- Whether the frame should carry the full entry list on every change (proposed, ≤50 entries) or incremental updates — full list is simpler and matches the diffing publisher; revisit only if frame size becomes a concern.
- Startup race: dispatch decisions before settings load use the defaults (enabled + both) — acceptable?
- Should the bell overlay be a router-driven app modal (proposed, matches /help and /settings) or component-local state (faster open, no history entry)?
