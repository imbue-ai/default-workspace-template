# Integrating the minds-options notification design into minds

Status: implemented (see `blueprint/minds-notification-system/plan-minds-notification-system.md`, the plan the implementation in `apps/minds` was built from). Kept as the background study behind that plan.

This document studies the notification design in the minds-options prototype
(https://imbue-ai.github.io/mind-sketches/prototypes/minds-options/, source in
`imbue-ai/mind-sketches` at `prototypes/minds-options/`) and plans how to bring
that design into the minds app. Findings are from reading the prototype source
(`src/shell/notifications.tsx`, `src/shell/PermissionOverlays.tsx`,
`src/screens/modals/NotificationCenter.tsx`, `src/components/Badge.tsx`,
`src/screens/Settings.tsx`) and from driving the published prototype in a
browser, plus a survey of `apps/minds` (frontend, Electron, Flask backend) and
the default-workspace-template `system_interface`.

## 1. Prototype design summary

### Architecture

One centralized notification feed (`NotificationsProvider`, a React context)
subscribes to the permissions event stream. Every event becomes a durable
`AppNotification` in a newest-first feed (capped at 50); a freshly arrived
notification may ALSO flash as a transient toast and/or an OS notification.
The feed is the durable home; toasts are only a "just now" flash. The stated
intent (module docstring): this replaces per-surface badging as the primary
"something happened" signal -- one bell count for the machine instead of
dotting every breadcrumb and switcher row.

### Notification model

- `AppNotification { id, tone, title, body, at, requestId?, request?, pool? }`
- Three tones, domain-specific rather than generic info/warn/error:
  - `waiting` (amber) -- a pending permission request, calmly waiting on the user
  - `breaking` (red) -- something already broken (an expired service sign-in
    blocking agents)
  - `success` (green) -- purely informational (sign-in reconnected)
- Red outranks amber wherever both apply.

### Surfaces (verified live in the browser)

1. **Titlebar bell** (right cluster, next to the bug-report button): a
   `TitlebarButton` with a red count pill (`Badge`, 99+ cap, 8px presence-dot
   variant also available). The count is **resolution-based, not view-based**:
   a request counts until approved/denied, a breaking sign-in until its pool
   reconnects, successes never count. Opening the feed does NOT clear it.
2. **Feed overlay** ("Notification center"): anchored panel under the bell
   (360px wide, max-height 70%, rounded-xl, `shadow-overlay`). Newest-first
   list; no clear button, no per-row dismiss. Rows:
   - Request rows render the shared `PermissionNotice` line ("<accent-dot>
     <workspace> requests access to <service-logo> <service>" + up to two
     lines of the agent's reason), a relative timestamp ("47s ago") led by a
     red dot while pending, and an Approved/Denied chip once resolved.
   - Credential/event rows: green check or red triangle-alert glyph + title +
     body + timestamp.
   - Resolved rows fade to a "spent receipt" (opacity-60 + grayscale, 500ms
     transition) so live asks stay the only vivid things.
   - While pending, the whole row is a button: it jumps to the asking
     workspace (repainting chrome accent) and opens the shared review modal.
   - Empty state: bell glyph + "You're all caught up."
3. **Toast stack**: floating cards at top-right under the titlebar (320px,
   `absolute right-3 top-[46px] z-[250]`).
   - 5s auto-dismiss (constant `TOAST_MS`); a corner X retires just the flash
     (the feed entry survives).
   - Request toasts reuse the same `PermissionNotice` row body; the whole card
     is clickable and acts like the in-chat card (jump to workspace + open
     review modal).
   - Breaking toasts get a red-tinted border, red title, and their own action
     button ("Reconnect...", with an "Opening browser..." busy state).
   - Stacking: newest in front; up to 3 peek behind at 4px slivers with
     scale/opacity falloff; anything deeper folds into an "N more" line.
     Hovering the pile fans it open into a full list (8px gaps); leaving
     re-collapses. Timers keep running throughout.
   - Animation: slide-down + fade-in on enter, slide-up + fade-out on exit
     (200ms ease-out, double-rAF so the enter transition actually plays).
4. **OS notifications** via the Web Notification API (best-effort; lazy
   permission request when the user opts in).

### Delivery preferences (Settings)

- "Notifications" on/off toggle -- "Nudge me when something needs me. The bell
  keeps its count either way." The feed always records; the preference only
  governs the extra nudge.
- When on, a style radio: `cards` (in-app toast stack) / `os` (system
  notification center) / `both`.

### Suppression rules (the design's core cleverness)

- A request raised by the workspace **currently on screen** does not toast --
  it is already surfaced inline (in-chat card); it still lands in the feed.
  (Verified live: the scripted Slack/Gmail asks raised while viewing the
  workspace produced badge count 2 with no toast.)
- No toast while the feed drawer is open; opening the drawer also retires any
  toasts already floating. You see one surface or the other, never both.
- A code comment flags multi-window: in the real app an in-app card should
  flash only in the OS-focused window, while the `os` style needs no gating
  (the system shows a single banner).

### Triggers (prototype scope)

Permission-domain only: new permission requests, service sign-in expiry,
sign-in reconnection. Other home-surface signals (machine Unhealthy chip,
"2 waiting on you" chip per machine card) exist but are separate surfaces,
not feed entries.

### Accessibility and misc.

- Toasts carry `role="status"`; close buttons have `aria-label`; the bell has
  `aria-expanded`; the settings radio is a proper `radiogroup`; decorative
  dots are `aria-hidden`.
- Relative timestamps are coarse ("just now", "3m ago"); one clock per render.
- Tech: React 19 + Tailwind v4 + lucide-react icons, Vite. Zero-logic mock
  data drives the demo.

## 2. Current minds notification capability

### The minds chrome (apps/minds: Mithril SPA + Flask backend + Electron)

**An OS-notification pipeline already exists end-to-end, but nothing in-app:**

- `POST /api/v1/agents/<agent_id>/notifications` (api_v1.py) lets an agent
  send `{message, title?, urgency?}` on its own behalf.
- `NotificationDispatcher` (desktop_client/notification.py) routes by runtime:
  Electron (JSONL event on backend stdout) > macOS (`osascript display
  notification`) > tkinter toast (bottom-right 320px window with urgency color
  stripe). Urgency: LOW/NORMAL/CRITICAL.
- Electron main (electron/backend.js:465 -> main.js:1695 `handleNotification`)
  builds a native `Notification`; click focuses the window already showing the
  workspace from `event.url`, else navigates the most recent window.
- Other producers: backup-setup failure (agent_creator.py:2991), backup trim
  (backup_trim.py:314).
- Gaps: fire-and-forget (no history, no in-app mirror); the HTTP request model
  (`AgentNotificationRequest`) drops the `url` field even though both the
  dispatcher and the Electron handler support it.

**In-app, the chrome has no toasts, no bell, no feed -- deliberately.**
`Titlebar.ts` documents "There is deliberately no requests entry here: a
pending permission request surfaces only as the centered popup..." and
`Titlebar.test.ts:58-67` asserts the titlebar contains no `requests-toggle` /
`requests-badge`. The prototype's design is precisely the overturn of this
stance.

What exists to build on:

- **Live state channel** `/ui/ws`: an edge-driven publisher (ui_publisher.py)
  diffs and broadcasts typed frames (`workspaces`, `accounts`, `providers`,
  `requests`, `health`, `discovery_health`) plus one-shot frames
  (`workspace_stopped`, `open_help`, `workspace_refresh`, `reload_ui`) via
  `publish_one_shot`. The frontend parser ignores unknown frame types
  (channel/messages.ts), so **adding a `notification` frame is backward
  compatible by construction**. Snapshot-on-connect seeds new windows.
- **Requests**: `UiRequestsMessage` carries bare pending request ids; the
  inbox model fetches per-id detail; surfaces today are the workspace's
  in-chat card, the Permissions tab's "Waiting on you" rows (with a
  `Badge` count), and the centered review popup (`shell.openRequestPopup`).
  The embed contract's `minds:permission-request-resolved` flips the in-chat
  card instantly.
- **UI primitives with exact prototype parity**: `Badge` (count pill, 99+ cap
  / 8px dot; breaking-red default), `Notice` + `noticeVariantClass`
  (info/warn/success/error tokens), `TitlebarButton`, the Shell overlay
  system (`RequestOverlay` is an anchored-under-titlebar precedent), and the
  "Reconnecting..." chip (`fixed top-[42px] right-2 z-[150]`) as the only
  free-floating transient overlay -- the positional precedent for a toast
  stack. All 26 color tokens the prototype uses exist verbatim in the
  chrome's style.css, as do `type-*` and `shadow-overlay` utilities; the
  visual design ports 1:1 at the token level.
- **Settings**: the `/settings` app modal with sections; the
  `report_unexpected_errors` toggle is the precedent for an app-level boolean
  preference persisted via `/ui/api/settings`.
- **Electron bridge**: typed `mindsNative` facade; a `shell-event`
  renderer->main relay with an allowlist (`KNOWN_SHELL_EVENT_TYPES`,
  main.js:1032) and sender-origin check. No dock badge/tray usage today.
- Missing bits: no bell glyph in icons.ts (30 icons; closest are `inbox`,
  `info`, `triangle-alert`); no credential-expiry signal on any frame
  (`ProviderPanelStatus` covers compute providers, not service sign-ins).

### system_interface (default-workspace-template; the workspace UI in the iframe)

Also Mithril 2 + Tailwind v4, but a separate codebase with its own token
scheme (`--color-*`, light-only, hand-written ~3700-line stylesheet) -- the
chrome's tokens/utilities do NOT exist there.

- **No notification/toast infrastructure.** The only banner is the dismissable
  `TerminalBanner`. The stated convention for failed user-initiated mutations
  is `window.alert()` (~13 call sites). Background failures (WS drop, model
  set, backfill) are console-only -- the user sees nothing.
- **Real-time plumbing that a notification system would tap**:
  - `/api/ws` (AgentManager.ts): `agents_updated` snapshots including per-agent
    `activity_state`; `addAgentActivityListener` **already computes
    THINKING -> IDLE transitions per agent** (effectively "agent finished"),
    currently rendered only as a tab liveness dot.
  - Per-agent SSE transcript: `assistant_message.is_api_error` /
    `is_auth_error` flags; `is_auth_error` already pushes straight into a
    modal (`openAgentAuth`) -- the one existing push-event-to-UI path.
  - `activityReporter` already reports open/visible tabs -- a ready-made
    "is the user looking at this chat" suppression signal.
- **Embed contract** (postMessage, single confined module, ratchet-enforced):
  workspace->embedder types today are `open-request-modal`, `open-help`,
  `open-ai-keys-page`, `bring-app-to-front`. No notification type. The
  compatibility policy is additive-only with silent ignore, so a new message
  type degrades cleanly against older chromes.
- No titlebar of its own (38px spacer under the chrome's), no settings screen.
  Changes here ride the vendored-contract sync and the `update-system-interface`
  deploy ritual, and multi-client addressing (per-client/layout/workspace)
  must be considered.

## 3. Proposed architecture

Recommendation: build the prototype's design in the **minds chrome** first
(apps/minds/frontend + desktop_client backend). That is where the prototype
literally places every surface (titlebar bell, anchored feed, toast stack over
the whole app), where the design tokens match 1:1, and where the event
sources already flow. The workspace side (system_interface) plugs in later as
an event *producer* through existing seams, not as a second notification UI.

### 3.1 Feed state: server-side, snapshot-seeded

Keep the durable feed in the backend, not per-window JS:

- New `UiNotificationEntry` pydantic model (id, tone, title, body, created_at,
  request_id?, workspace_agent_id?, url?) + a bounded deque (cap 50) owned by
  the desktop_client, included in `UiSnapshot` so every window (and a reload)
  seeds the same feed, and a one-shot `notification` frame for live arrivals.
  The tolerant frame parser makes this purely additive.
- Rationale: the chrome is multi-window (and works in plain browser tabs); the
  prototype's in-memory context would give each window a different history and
  count. Snapshot-on-connect is already the established pattern.

### 3.2 Producers (what lands in the feed)

Phase 1 (parity with the prototype's scope plus the existing pipeline):

- **Permission requests** (`waiting` tone): derive on the backend where
  requests are already tracked -- when a new pending request appears, append a
  feed entry carrying the same display fields the inbox card already exposes
  (workspace name, accent, kind label / service). The frontend must not have
  to fetch per-id detail just to render a feed row. Resolution state is NOT
  duplicated: the unresolved test joins the feed entry against the live
  `requests` frame (an entry with `request_id` counts while that id is still
  pending), exactly as the prototype joins against its store.
- **Agent-sent notifications** (existing `POST /api/v1/agents/<id>/notifications`):
  route through the same feed (tone from urgency: LOW->success-ish info,
  NORMAL->waiting-style, CRITICAL->breaking) in addition to the existing
  dispatcher, so these finally have an in-app home and history. Add the
  missing `url` field to the HTTP model while touching it.
- **Backup failures** (already OS-notified): same feed append at the existing
  dispatch sites.
- **Deferred (no wire signal exists today)**: the prototype's
  `breaking`/`success` credential expiry/reconnect pair. There is no live
  service-credential-expiry event anywhere in minds today; producing one
  belongs to latchkey/connector work and should be its own project. The tone
  and row rendering should be built now so the producer can plug in later.

### 3.3 Frontend (Mithril ports of the prototype components)

- `models/notifications.ts`: applies snapshot + `notification` frames;
  derives `unreadCount` (join vs. `RequestsStore` pending ids; breaking
  entries count until their condition clears; successes never); holds the
  live-toast id set and per-window suppression state. Plain class + m.redraw,
  like every other store.
- **Bell** in the Titlebar right cluster (`TitlebarButton` + `Badge` count
  pill; add a `bell` glyph to icons.ts alongside the other Lucide-copied
  paths). Requires deleting the Titlebar.test.ts assertions and the "no
  requests entry" comment -- see open question 2.
- **Feed overlay**: an anchored panel following the `RequestOverlay`
  precedent (route-driven like other app modals, e.g. `/notifications`).
  Request rows share markup with the Permissions tab's "Waiting on you" rows;
  clicking a pending row = `m.route.set` to the workspace + `openRequestPopup`
  (the shell's existing review popup), mirroring `useReviewInWorkspace`.
- **Toast layer** in Shell: fixed stack at the Reconnecting-chip position
  (top-right under the titlebar; stack the chip and toasts in one column so
  they never overlap). 5s auto-dismiss, corner X, click-through to review.
  Suppression parity: skip the flash when the entry's workspace is the
  current route (`classifyRoute` gives this), when the feed overlay is open,
  and (Electron) when the window is not focused -- the prototype comment's
  multi-window rule, implementable via existing focus events or
  `document.hasFocus()`.
- **Settings**: a Notifications section (toggle + cards/os/both radio) in
  SettingsSections, persisted server-side next to `report_unexpected_errors`.
  Server-side persistence also lets the backend decide OS dispatch (below).

### 3.4 OS delivery: reuse the dispatcher, not the Web Notification API

The prototype uses the browser Notification API because it is a browser-only
sandbox. minds already has something strictly better: the
`NotificationDispatcher` with native Electron notifications and
click-to-navigate. Proposal: the backend consults the stored preference at
publish time -- `cards`/`both` sends the `/ui/ws` frame (in-app flash),
`os`/`both` invokes the dispatcher. The renderer never touches the
Notification API; plain-browser mode simply has in-app cards only (or we add
Web-API delivery for browser mode later). This keeps one OS banner regardless
of how many windows are open.

### 3.5 Workspace-side events (phase 2)

To surface agent-level events (turn finished, API error, socket drop) when
their chat tab is not visible, the workspace should *produce* events, not
render its own toasts:

- Preferred seam: a new additive embed-contract message
  (`minds:notify { tone, title, body, agentId }`), workspace -> embedder,
  following the `PERMISSION_REQUEST_RESOLVED` probe-with-fallback pattern for
  vendored-contract skew. The chrome feeds it into the same store/feed. The
  contract's tolerant policy makes old chromes ignore it safely.
- The signals already exist client-side in system_interface
  (`addAgentActivityListener` THINKING->IDLE, `is_api_error` on SSE), and
  `activityReporter`'s open/visible sets provide "don't notify about what the
  user is looking at" suppression.
- Alternative (server-side POST to the desktop client API) works for
  headless-agent cases but adds auth plumbing; the in-iframe postMessage path
  costs nothing and is scoped to UI-observed events. Direct share visits
  (no chrome) simply have no feed -- same degradation as other contract
  features.

### 3.6 What NOT to port

- The peek-stack/hover-fan/"N more" toast choreography: propose a plain
  vertical stack (newest on top, cap ~3 + count line) for v1 -- the
  absolute-positioned morph is the most intricate part of the prototype and
  pure polish. See open question 4.
- The prototype's per-surface suppression of home-card chips ("2 waiting on
  you") -- the chrome's home page does not have these today; out of scope.

## 4. Gaps and dependencies

- **Framework mismatch**: prototype is React 19 + lucide-react; both minds
  UIs are Mithril 2. Every component is a re-implementation (markup and
  behavior port), not a reuse. Tailwind class strings and tokens port nearly
  verbatim for the chrome; they do NOT port to system_interface (different
  token scheme) -- another reason to keep notification UI chrome-only.
- **No bell icon** in the chrome's icons.ts (or system_interface's icon set);
  add the Lucide path like the existing copies.
- **`UiRequestsMessage` is bare ids**: feed entries need display fields at
  append time (workspace name/accent, request title/summary) so rows render
  without per-id fetches.
- **No credential-expiry signal exists** (checked ui_models, providers frame,
  settings API): the `breaking`/`success` pair needs a latchkey-side producer
  eventually; only the rendering lands now.
- **Design-stance reversal**: Titlebar.test.ts:58-67 + the Titlebar comment
  and the requests.ts "never opens anything on its own" doc must be updated
  with the decision.
- **Multi-window duplicate suppression** for in-app cards (focused-window
  gating); OS banners are naturally single.
- **Animations**: plain CSS transitions, no library needed, but Mithril's
  lifecycle (oncreate/onbeforeremove) replaces the double-rAF React dance.
- **Accessibility**: port role="status", aria-expanded, aria-labels,
  radiogroup semantics.
- **Phase-2 dependencies**: embed-contract version bump + vendored sync,
  system_interface deploy ritual, multi-client semantics (which client shows
  a toast for a workspace-global event).
- **Testing**: unit tests for the store/derivations (vitest, like existing
  frontend tests); the interactive toast behavior is tmux/manual territory
  per repo convention; backend feed/publisher tests in Python.

## 5. Open questions for Preston

1. **Primary surface**: chrome-only for v1 (recommended above), with
   system_interface as a phase-2 event producer via the embed contract? Or do
   you want in-workspace toasts too (visible on direct share visits without
   the chrome)?
2. **The titlebar stance**: the current design deliberately keeps requests out
   of the titlebar (test-enforced); the prototype's bell explicitly replaces
   that with one persistent count + feed. Confirm you want the overturn
   (bell + resolution-based badge in the titlebar right cluster).
3. **Trigger set for v1**: permission requests + agent-sent notifications +
   backup failures (all have wire signals today)? Credential expiry/reconnect
   is rendering-only until a latchkey producer exists -- OK to defer? Should
   workspace health transitions (machine Unhealthy) also become feed entries,
   or stay a home-card/notice-band concern?
4. **Toast fidelity**: simple vertical stack v1, or is the prototype's
   peek/hover-fan stacking part of the design you want preserved?
5. **OS delivery path**: agree the backend dispatcher (native notifications,
   click-to-navigate) is the OS channel and the renderer never uses the Web
   Notification API? And should agent-initiated notifications
   (POST /api/v1/.../notifications) start appearing in the in-app feed as
   proposed, or stay OS-only?
6. **History durability**: server-side bounded feed (survives reloads, shared
   across windows -- recommended) vs. the prototype's per-window in-memory
   feed? Any need to persist across app restarts (currently proposed: no,
   in-memory in the backend process)?
7. **Scope/priority**: is this a small v1 (bell + feed + toasts for the
   trigger set above, settings toggle) or should it grow toward a fuller
   notification center (filtering, per-workspace mute, actions beyond
   review/reconnect)? Any timeline pressure that argues for cutting the
   settings UI from v1 (default: enabled, cards)?

## Appendix: prototype behaviors verified live

- Bell badge appeared with count 2 when the scripted Slack/Gmail requests
  raised while viewing the workspace -- and no toast flashed (on-screen
  workspace suppression).
- Approving Slack dropped the count to 1 mid-flow (resolution-based count),
  and abandoning the simulated sign-in returned it to 2.
- Feed rows render exactly as coded: red pending dot + relative timestamp,
  accent dot + bolded workspace/service line, two-line clamped reason.
- Clicking a feed row closed the feed, jumped to the workspace (chrome accent
  repaint), and opened the review modal over it.
- Home machine cards carry separate "2 waiting on you" / "Unhealthy" chips --
  adjacent signals, not feed entries.
