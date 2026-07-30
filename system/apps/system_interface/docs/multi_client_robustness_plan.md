# System interface multi-client robustness plan

Prioritized remediation plan from the 2026-07-30 investigation of system-interface
misbehavior with two concurrent clients (minds desktop app + a Cloudflare
"share the whole machine" browser tab, both serving the same
`system_interface` Flask process), plus a single-client chat-paging wedge
reproduced the same day. Evidence was gathered live from the production
workspace `geebspace`; pointers are in the appendix.

Ordering rule: where two causes compound each other, the simpler fix (fewer
design decisions) comes first. P0 items are mechanical and independently
shippable; P1 items need small product choices; P2 items need real design.

## Symptom -> cause map

| Symptom | Cause(s) |
|---|---|
| Interacting with one client rearranges/reloads the other (tabs yanked, chats flash "Loading events...", terminals reconnect) | Shared `desktop` layout live-sync: every autosave makes the other client rebuild its whole dockview (P1.5, P1.7, P2.8) |
| "No conversation data" tab + endless `screen?scrollback=true` 404 spam | Destroyed agent's chat tab persists in the saved layout; not-found state is permanent and the screen poll refires every redraw (P0.1, P0.2) |
| Chat transcript frozen while the TUI keeps moving | SSE stream dies silently, no client-side liveness check (P0.4); layout rebuilds interrupting streams (P1.7) |
| Chat tab stuck on "Loading messages...", `/events?after=<tail>` spam, then inert | Paging state corrupted by a stale response applied across a snapshot reset; no backoff; un-timeboxed fetch pins `backfillInFlight` (P0.3) |
| Agent-driven `layout.py refresh` / `close` silently does nothing, or closed panels come back | Fire-and-forget broadcasts with no delivery confirmation or replay; clients that missed an op autosave the old state back (P2.9) |
| Terminal tabs retitle/rebind oddly, layout saves churn | Live title tracking (tmux hooks -> notify -> broadcast -> `scheduleSave` on every client) + per-apply `terminalId` re-minting (P1.5, P1.6) |

## P0 — mechanical fixes, no design decisions

### P0.1 Stop the destroyed-agent 404 storm

`ChatPanel.ts` `fetchScreenCapture` refires on every mithril redraw once a
fetch errors (the guard only skips when `screenContent !== null`, and errors
leave it null). Observed: 13,582 `screen?scrollback=true` 404s in one day,
bursts of ~30/s, each completion triggering another global redraw — a
self-sustaining redraw/fetch loop that runs even while the tab is hidden
(dockview keeps inactive panels mounted).

- Cache the error outcome the same way content is cached; retry at most on an
  explicit user action or a slow (>=30s) timer.
- Recover from not-found: clear the `notFoundAgentIds` tombstone and retry
  `/events` when the agent id (re)appears in an `agents_updated` snapshot.
  Today `ensureAgentLoaded` never refetches for the same agent id, so a
  transient 404 wedges the panel until a hard refresh.

Acceptance: a chat tab for a nonexistent agent settles to a static
"No conversation data" state with zero background requests; if the agent
reappears, the tab recovers without a reload.

### P0.2 Prune panels whose agent no longer exists

Nothing removes a destroyed agent's panels: UI-destroy removes the panel only
on the clicking client, CLI/worker destroys remove it nowhere, and saved
layouts resurrect it on every apply (the geebspace 404 storm began the second
a client freshly mounted the saved layout containing the destroyed
`migrate-workspace` tab).

- On `agents_updated` and during `applyLayoutContent`, strip chat panels whose
  agent id is absent from the known-agent list (mirror the existing
  primary-agent strip), or replace them with an explicit "agent destroyed"
  placeholder that has a close button. Either is acceptable; strip-plus-toast
  is the smaller change.
- Care: `agents_updated` can transiently miss agents while the `mngr observe`
  pipeline restarts. Only prune when the agent is absent AND its `/events`
  fetch 404s (the server distinguishes a discovery gap from a genuinely
  unknown agent via `_find_agent`), or debounce pruning across a couple of
  snapshots.

Acceptance: destroying an agent (from UI, CLI, or another client) removes or
tombstones its tabs on every connected client and from the saved layout after
the next autosave; dead tabs never survive a reload.

### P0.3 Fence transcript paging against resets and hangs

Reproduced single-client wedge: a scroll-up backfill was in flight when an
SSE-reconnect snapshot (`reconnectWithSnapshot` -> `fetchEvents` ->
`placeWindow`) reset the window to the live tail; the stale `prepend()`
landed afterwards and glued a non-contiguous older page above the tail with
its old-epoch `firstOffset`. Result: `firstOffset + heldCount < total`
forever while the window's last event IS the tail -> infinite
`/events?after=<tail>` spam (server correctly answers
`{"events":[],"offset":591,"total":591}`), then a hung request pinned
`backfillInFlight` (no timeout) and froze the panel on the
"Loading messages..." overlay with scrolling inert.

In `Response.ts` / `ChatPanel.ts`:

- Generation counter on `TranscriptStore`, bumped by `reset()`. In-flight
  backfill/forward/jump responses capture the generation at request time and
  are discarded on mismatch. (Equivalent alternative: abort in-flight paging
  whenever a snapshot reset happens.)
- Contiguity guard: `prepend()` only accepts a page whose end abuts the
  current window start (and `appendForward()` the mirror); non-abutting pages
  are dropped and the fetch retried from current coordinates.
- Snap-to-tail: when `after=<our last event>` returns empty with
  `offset == total`, treat it as authoritative — reconcile so
  `firstOffset + heldCount == total` (or refetch the snapshot if the held
  window cannot be made consistent).
- Timeouts on all paging requests; on timeout reset `backfillInFlight` (and
  `pendingPinToWindowTop`). Exponential backoff after consecutive
  no-progress pages instead of refiring on every redraw.

Acceptance: with artificial request delays/hangs injected, the panel never
issues the same cursor query more than a few times, never freezes with the
overlay up, and a snapshot reset racing a backfill leaves a consistent
window.

### P0.4 SSE liveness watchdog

A half-dead SSE connection (tunnel drop, sleep/wake) freezes the transcript
silently while the ttyd WebSocket (which has `ping_interval` keepalive) keeps
the terminal alive — the classic "chat out of sync with the TUI". The server
already emits `: keepalive` every ~8s, but those are SSE comments, which the
browser `EventSource` API does not surface, so the client cannot watch them.

- Server: change the keepalive to a real event (e.g. `event: ping` with an
  empty data payload) in `_stream_filtered_events`.
- Client (`StreamingMessage.ts`): track last-received time (any message or
  ping); if silent for ~30s, close and go through `reconnectWithSnapshot`.

Acceptance: killing the underlying TCP connection without a FIN (e.g. drop
via a proxy) recovers the transcript within ~35s with no user action.

## P1 — small decisions, big two-client payoff

### P1.5 Make layout content convergent (defuse the autosave ping-pong)

All desktop-class clients share the `desktop` layout. Autosave fires from
`onDidLayoutChange` (1.5s debounce); the server broadcasts `layout_saved`;
the other client re-applies. The existing echo suppression (originator skip +
4s window + content-equality guard) cannot converge because the serialized
content is client-specific, so every interaction on either client re-triggers
a save and a remote rebuild. Observed: `POST /api/layouts/desktop` every 1-2
seconds with exactly two clients connected.

Remove the non-convergence feeders (each is small):

- Stop re-minting `terminalId` (and the terminal URL embedding it) inside
  `applyLayoutContent` — mint only when absent, or better, derive it at
  iframe-mount time and keep it out of the serialized `panelParams`
  entirely. Today every remote apply changes the layout content by
  construction, guaranteeing the next save differs.
- Compare a normalized projection in the content guard: exclude volatile /
  client-specific fields (terminal ids, urls that embed them, pixel
  sizes, active-tab state) from the equality check so a remote apply that
  changed nothing semantic does not arm the next save.
- Stop calling `scheduleSave()` from `terminal_session` broadcasts (every
  client currently persists a layout save because a tmux title changed —
  and the broadcast goes to all clients). Titles are re-derivable state.

Acceptance: two clients with different window sizes idling on the same
layout reach quiescence (zero autosave POSTs) within one debounce interval
after any single interaction, instead of trading saves indefinitely.

### P1.6 Dumb down terminal tabs (remove the "smart" machinery)

The live terminal title tracking is the most fragile cross-client machinery
in the app, and its value is one cosmetic feature (tab titles following tmux
session switches/renames). Current pipeline: tmux `set-hook`
`client-session-changed` / `session-renamed` (`terminal_tmux.conf`) ->
`notify_terminal_session.py` -> `POST /api/terminals/notify` -> resolve the
dockview tab via the pty->terminal-id files `session.sh` maintains under
`commands/ttyd/clients/` (with explicit pty-reuse shadow handling, a known
race) -> `terminal_session` WS broadcast -> every client retitles and
autosaves. With two clients attached to the same tmux session this
misattributes and churns; combined with P1.5's terminal-id re-minting it is
a primary feeder of the layout storm.

Proposal (mostly deletion):

- Remove the tmux hooks, `notify_terminal_session.py`, the
  `/api/terminals/notify` endpoint, the `terminal_session` WS event and its
  frontend handler, and the `clients/` pty-mapping block in `session.sh`.
- Drop `terminalId` from panel params and the ttyd URL (composes with P1.5).
- Terminal tabs become "attach to `<session name>`" with the session name as
  a static title. If rename-following matters, offer an explicit rename
  action in the tab context menu that renames the tmux session and retitles
  the tab in one deliberate step.
- Leave `window-size latest` (set in `terminal_tmux.conf`) alone. It was
  added deliberately to fix agent terminals being resized to absurdly small
  sizes (a stale or default-80x24 client shrinking the session for
  everyone, which is what smallest-style sizing does). With two live
  clients it does mean the most-recently-active client dictates the size of
  a shared session, but that is inherent to sharing one tmux session, and
  the churn users actually see comes from the reconnect storms
  (P1.5/P1.7), not this setting. Do not revert it as part of this work.

Acceptance: with two clients viewing the same terminal, no
`terminal_session` traffic exists, no layout saves are triggered by terminal
activity, and reconnecting one client does not disturb the other's terminal
beyond tmux's inherent shared-session semantics.

### P1.7 Apply remote layout changes incrementally

`applyLayoutContent` does `dv.clear()` + `fromJSON()` — a full teardown of
every panel — for every remote save. That is what makes the other client's
interaction *feel* catastrophic: all chats remount (SSE disconnect, snapshot
refetch, scroll reset), all iframes/terminals reload, and the active tab
jumps to the saver's.

- Diff the incoming saved layout against the current dockview: add missing
  panels, remove departed ones, move/resize the rest, and leave untouched
  panels mounted. Never adopt the remote active-tab.
- Cheap first stage (worth shipping alone): skip the apply entirely when the
  normalized content (per P1.5) is semantically equal, and preserve panels
  whose params are unchanged.

This is the biggest jank reducer for the two-client case, but it needs
careful dockview work — hence P1 behind the mechanical P0s, and staged.

Acceptance: a tab opened on client A appears on client B without B's other
panels remounting (iframe `src` unchanged, chat SSE uninterrupted, scroll
position preserved, B's active tab unchanged).

## P2 — needs design

### P2.8 Multi-client layout semantics

The current model (one shared mutable layout per device class, live-synced,
with client-specific geometry serialized into it) guarantees two same-class
clients fight. Decide the intended semantics:

- Per-client working copies with explicit save/load (layouts become named
  snapshots, not live-shared state); or
- Truly shared layouts, but with client-local geometry (store proportions or
  per-client size overlays; never sync active-tab); or
- Last-writer-wins snapshots applied only on explicit load.

Also decide whether a brand-new client joining (e.g. opening the share URL)
should adopt the existing layout read-only by default rather than
immediately becoming a co-writer — the geebspace incident started the moment
the second client's first autosave landed.

### P2.9 Reliable layout/broadcast delivery (versioning + acks)

Broadcasts are fire-and-forget: `layout.py refresh` at 22:35:03 and 22:40:05
landed while the desktop client's WS was between drops (fresh registrations
at 22:37:32 / 22:41:22) and silently did nothing; `layout.py close` of the
dead tab was undone because a client that missed it autosaved the panel
back.

- Version the layout document. Autosaves carry the base version; the server
  rejects saves from a stale base (409 -> client refetches and reapplies its
  local delta or drops it). This alone kills zombie-panel resurrection.
- On WS reconnect, clients re-fetch the layout registry + current version
  instead of assuming continuity.
- Agent-driven ops (`refresh`, `close`, ...) report delivery: the endpoint
  already knows the registered clients; return delivered-to-N (and to whom)
  so `layout.py` can tell the agent "no client received this" instead of
  claiming success.

### P2.10 Server-side hygiene (single-client wins, lower urgency)

- `app_context.get_or_create_watcher`'s `on_events` calls
  `watcher.get_all_events()` (full-transcript body resolution) on every poll
  batch to recompute activity state — O(transcript) disk work per new event
  on long conversations. Make activity tracking incremental.
- Distinguish "agent genuinely unknown" from "agent manager not primed /
  observe pipeline restarting" in per-agent endpoints (404 vs 503). Clients
  then know whether to tombstone (P0.2) or retry.

## Diagnostics to add alongside (cheap)

- Log `client_id` on layout autosave POSTs so save storms are attributable
  from the access log alone.
- Frontend: console.warn + counter when paging makes no progress N times;
  when the SSE watchdog fires; when a layout re-apply is triggered remotely.
- Consider a Sentry breadcrumb/event for watchdog-triggered SSE reconnects
  exceeding a rate, mirroring the existing minds transport-blip reporting.

## Appendix: evidence (geebspace, 2026-07-30, times UTC)

- Two desktop clients on layout `desktop`:
  `workspace_layout/events/client_activity/events.jsonl` shows
  `aa57b870...` (desktop app, connected 18:37) and `a65b7110...` (share
  tab, connected 21:05); server log `layout op=context ... clients=2`.
- Autosave storm: `POST /api/layouts/desktop` every 1-2s in
  `/var/log/supervisor/system_interface-stderr.log` during two-client use.
- Dead-tab 404 storm: 13,582 `GET /api/agents/agent-f392.../screen` 404s
  (plus 232 `/events` 404s) starting 21:05:58 — the second the share tab
  first mounted the saved layout; `layouts/desktop.json` still contained
  `chat-agent-f392...` titled `migrate-workspace` (a destroyed worker), and
  an agent-driven `layout.py close` at 22:58 did not stick.
- Lost refreshes: `layout.py refresh daily-digest` at 22:35:03 and 22:40:05
  vs fresh WS registrations at 22:37:32 and 22:41:22.
- Single-client paging wedge: live probe of `/api/agents/agent-17f5.../events`
  returned `{"events":[],"offset":591,"total":591}` for the exact cursor the
  client was spamming, and the tail query confirmed that cursor was global
  index 590 of 591 — server consistent, client window arithmetic corrupted.
