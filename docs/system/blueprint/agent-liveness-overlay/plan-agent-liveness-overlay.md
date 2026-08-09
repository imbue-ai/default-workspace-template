# Plan: agent liveness overlay (the "if you can interact, it's live" contract)

Status: plan only, not approved for build yet.

## The contract

If the user can touch an interactive control (composer, model bar picker,
effort/fast, interrupt, shoulder-tap), the agent is RUNNING and ready to
receive. Reading the transcript is always allowed. Liveness is made visible
instead of implicit.

Motivating cases: a container restart leaves agents stopped and not
auto-resumed; a manual `mngr stop` for testing; any future path that stops an
agent. Today the composer on a stopped agent silently auto-starts on send
(works, but indistinguishable from a hang), and a model switch on a stopped pi
agent is silently dropped (`pi_control.jsonl` baseline swallows pre-start
intents).

## Overlay states (exactly three, derived, no timers driving UI)

The overlay covers the ENTIRE bottom interaction region of the chat panel:
composer, attach/send controls, and the under-bar row (model bar, powered-by
credit, "Open agent terminal", "Agent auth"). The transcript above stays fully
readable and scrollable.

```
state = f(pushed_lifecycle, local_pending_start)

RUNNING and no pending start   -> no overlay; everything interactive
STOPPED and no pending start   -> "Agent stopped -- click to resume" (whole
                                  region is one click target)
pending start (any pushed
state until RUNNING arrives)   -> "Starting agent..." loading overlay
start failed                   -> "Couldn't start -- click to retry" + detail
```

- `pushed_lifecycle` is `AgentState.state` -- ALREADY on the frontend store,
  pushed on every `agents_updated` (fed by `mngr observe`, real probed
  lifecycle incl. RUNNING -> STOPPED on process death). Nothing reads it today;
  this feature is its first consumer.
- `local_pending_start`: set when the overlay click fires
  `POST /api/agents/:id/start` (endpoint exists; same in-process mngr start
  path as message delivery; no-op when already running). Cleared when a push
  reports RUNNING, or on POST error (-> retry state), or by a failsafe timeout
  (~90s -> retry state; mngr starts can legitimately take a while on a cold
  container).
- If something ELSE starts the agent (terminal open, internal resend, another
  client), the RUNNING push clears the overlay with no local involvement.
  Symmetric: an external `mngr stop` flips the composer to the stopped overlay
  on the next push.

## Scope

Frontend (the bulk):

- `ChatPanel.ts`: derive the overlay state, render the overlay over the
  interaction region, wire the click -> `startAgent(agentId)` (new tiny
  `Response.ts` helper hitting the existing endpoint), hold
  `pendingStartAgentIds` locally.
- No new wire fields, no new WebSocket events, no backend polling.

Backend (small, contract belt-and-braces -- the UI can no longer reach these,
but the API can):

- `POST /api/agents/:id/model` and the shoulder-tap/flush endpoint return 409
  when the agent's lifecycle state is not RUNNING (message send keeps its
  auto-start behavior -- internal senders like the welcome resend rely on it).

Out of scope (deliberately, "bit by bit"):

- The interrupt-button redesign (native in-TUI keypress instead of process
  restart) -- separate proposal, pending approval.
- Pi control-file consume-by-rename (the crash-window between append and
  apply). The overlay structurally prevents the UI-originated stopped-switch;
  the residual window is a live-agent crash mid-switch, rare. Follow-up.
- Auto-start-on-tab-visit. Rejected: browsing history should not boot
  processes.
- Proto-agents (mid-creation) -- they already have their own creation UI flow.

## Open items to verify at build time

1. Enumerate the exact `state` strings mngr observe pushes (RUNNING/STOPPED
   confirmed; check for CREATING/UNKNOWN/others) and decide the overlay bucket
   for each non-RUNNING value (default: treat as stopped, label generically
   "Agent not running -- click to resume").
2. Whether `POST /start` blocks long enough that its 200 can race the RUNNING
   push (harmless either way -- both clear pending -- but decide which one is
   authoritative; recommendation: the push).
3. The under-bar links ("Open agent terminal") currently trigger their own
   start -- under the overlay they are unreachable when stopped; confirm the
   terminal-tab restore path (saved layouts) still starts agents itself.
4. Stopped-agent model bar: chip renders last-known state from
   `minds_model_state.json` (already the case); confirm the picker is inert
   under the overlay rather than hidden (keep the chip visible, read-only).

## Build mechanics (when approved)

system_interface change -> isolated worker via update-system-interface flow,
self-verified preview against a really-stopped agent (`mngr stop` a scratch
chat agent, view its tab, click resume, watch the three states), then merge +
reveal (frontend-only reveal: bundle rebuild + tab reload broadcast; backend
409 guards restart the service). Estimated: small -- one worker pass.
