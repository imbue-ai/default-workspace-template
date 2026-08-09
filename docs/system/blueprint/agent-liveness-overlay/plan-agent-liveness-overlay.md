# Plan: agent liveness overlay (the "if you can interact, it's live" contract)

Status: explored and specified; pending adversarial review + user go.

## The contract

If the user can touch an interactive control (composer, model bar picker,
effort/fast, interrupt/stop, shoulder-tap, under-bar actions), the agent is
alive and ready to receive. Reading the transcript is always allowed. Liveness
is visible, never implicit.

Motivating cases: container restart leaves agents stopped and un-resumed;
manual `mngr stop`; process death mid-session. Today the composer on a stopped
agent silently auto-starts on send (works, but reads as a hang), and a model
switch on a stopped pi agent is silently dropped.

## Ground truth from exploration (verified file:line)

- The frontend ALREADY receives lifecycle state: `AgentState.state`
  (`frontend/src/models/AgentManager.ts:18`), pushed on every `agents_updated`.
  Fed by `mngr observe` (`agent_manager.py:978-1046`): event-driven upserts
  carrying `AgentDetails.state.value`, including RUNNING -> STOPPED on process
  death. Nothing in the frontend reads it today -- this feature is its first
  consumer.
- Full state alphabet (`mngr/primitives.py:282-297`): STOPPED, RUNNING,
  WAITING, REPLACED, RUNNING_UNKNOWN_AGENT_TYPE, DONE, UNKNOWN.
- mngr's own liveness predicate (`agents/base_agent.py:204-212`): alive =
  {RUNNING, WAITING, REPLACED, RUNNING_UNKNOWN_AGENT_TYPE}. The overlay MUST
  mirror this exact predicate (do not invent a second liveness definition).
  Buckets:
  - alive set above -> no overlay
  - STOPPED, DONE -> stopped overlay ("Agent stopped -- click to resume")
  - UNKNOWN (provider unreachable, sticky) -> stopped overlay with generic
    copy ("Agent unavailable -- click to retry"); a start attempt will fail
    into the retry state, which is the honest surface for it.
- Start path exists end-to-end: `POST /api/agents/:id/start`
  (`server.py:1652-1677`) -> `agent_discovery.start_agent` -> mngr's
  `ensure_agent_started` (same in-process path message delivery uses; no-op
  when already running). Synchronous: 200 means started (or already running),
  500 carries the error detail.
- The overlay target is exactly the `footer.app-footer` block
  (`ChatPanel.ts:868-903`): ActivityIndicator + MessageInput +
  `composer-under-bar` (ModelBar, "Open agent terminal", "Agent auth",
  PoweredByCredit). One region, one overlay.
- Proto-agents already render NO footer (`isProtoAgent` guard,
  `ChatPanel.ts:865`) -- out of scope for free.
- Saved-layout terminal tabs start agents themselves
  (`AgentTerminalPanel.ts:29` `ensureAgentStarted`) -- unaffected by gating
  the chat footer.
- Model chip on a stopped agent keeps rendering last-known state (the
  `minds_model_state.json` file persists) -- desired; the overlay makes it
  read-only rather than hidden.

## Overlay state machine (pure derivation, no UI-driving timers)

```
alive(state)        = state in {RUNNING, WAITING, REPLACED, RUNNING_UNKNOWN_AGENT_TYPE}

no pending start:
  alive             -> no overlay
  STOPPED | DONE    -> overlay: "Agent stopped -- click to resume"
  UNKNOWN           -> overlay: "Agent unavailable -- click to retry"
pending start:      -> overlay: "Starting agent..." (spinner; not clickable)
start POST failed   -> overlay: "Couldn't start -- click to retry" + detail
```

- `pending start` is a frontend-local `Set<agentId>`: set on overlay click
  (fires `POST /api/agents/:id/start`), cleared by EITHER an alive push OR the
  POST resolving (error -> retry state). The push is authoritative for
  clearing to "no overlay"; the POST 200 alone keeps the spinner until the
  push lands (observe latency is small; a ~90s failsafe flips to retry so a
  wedged start cannot spin forever -- the only timer, and it only affects the
  failure surface).
- External transitions need no local involvement: another surface starting the
  agent (terminal open, internal resend, another client) clears the overlay
  via the push; an external `mngr stop` raises it the same way.
- The stopped overlay is ONE click target covering the whole footer, keyboard
  reachable (a real button), aria-labelled.

## Scope

Frontend (the bulk):

- `ChatPanel.ts`: derive the overlay state, wrap/cover the footer block,
  render the three overlay variants, `pendingStartAgentIds` module state, and
  a small `startAgent(agentId)` helper in `models/Response.ts` hitting the
  existing endpoint.
- ActivityIndicator inside the covered region: when the overlay is up the
  indicator is stale by definition (backend already re-gates activity on
  lifecycle change, `agent_manager.py:1058-1066`) -- covered, not special-cased.

Backend (belt-and-braces; the UI can no longer reach these, the API still can):

- `POST /api/agents/:id/model` and the shoulder-tap/flush endpoint return 409
  when the agent is not in the alive set. Message send keeps its auto-start
  (internal senders -- welcome resend, cross-agent messaging -- rely on it).
  Reuse the same alive predicate server-side; do not fork it.

Out of scope (bit by bit):

- Interrupt-button redesign (native in-TUI keypress) -- separate proposal.
- Pi control-file consume-by-rename (residual crash-window) -- follow-up;
  the overlay structurally prevents the UI-originated stopped-switch.
- Auto-start on tab visit -- rejected: browsing must not boot processes.
- Any change to internal auto-start semantics.

## Verification (build time)

- Unit: overlay derivation table (each lifecycle value x pending flag).
- Manual, against a real stopped agent (`mngr stop` a scratch chat agent):
  stopped overlay renders, transcript scrolls, chip shows last-known model,
  click -> spinner -> footer unlocks on the RUNNING push; `mngr stop` again
  while viewing -> overlay returns; kill the start mid-flight -> retry state.
- The 409 guards: switch a stopped agent via curl -> 409; message send on a
  stopped agent still auto-starts.

## Build mechanics (when approved)

system_interface change -> isolated worker (update-system-interface flow),
self-verified preview against a genuinely stopped agent, merge + reveal
(frontend bundle rebuild + tab reload; backend guard change restarts the
service). One worker pass, small.
