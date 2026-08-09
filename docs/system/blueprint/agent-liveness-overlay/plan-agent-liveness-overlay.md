# Plan: agent liveness overlay (the "if you can interact, it's live" contract)

Status: v3 -- explored, adversarially reviewed (2 passes), amendments folded.
Pending user go.

## The contract

If the user can touch a COMPOSING control (composer, model bar picker,
effort/fast), the agent is alive and ready to receive. Reading the transcript
is always allowed. Liveness is visible, never implicit.

Explicitly NOT under the contract (review outcome): recovery controls stay
reachable on a dead agent -- the shoulder-tap (it restarts the agent by
design and is a one-click recovery for a dead agent with queued messages),
"Open agent terminal" (already a one-click start-and-attach), and "Agent auth"
(liveness-independent; most needed exactly when the agent died on an auth
error).

Motivating cases: container restart leaves agents stopped; manual `mngr stop`;
process death mid-session (lifecycle DONE); the silent pi stopped-switch drop.

## Ground truth (verified; corrected after review)

- `AgentState.state` arrives on every push and on the WS connect seed
  (`agent_manager.py:576-592`, `server.py:1503-1510`); fed by `mngr observe`
  event stream including RUNNING -> STOPPED on process death.
- It already HAS a frontend consumer: the per-tab liveness dot
  (`frontend/src/models/agentLiveness.ts`, used by `DockviewWorkspace.ts:417-457`),
  whose alive set is {RUNNING, WAITING, RUNNING_UNKNOWN_AGENT_TYPE} (REPLACED
  renders dormant). The overlay MUST share one predicate with the dot -- two
  disagreeing liveness displays on one screen is the failure mode this plan
  exists to remove.
- Full state alphabet (`mngr/primitives.py:282-297`): STOPPED, RUNNING,
  WAITING, REPLACED, RUNNING_UNKNOWN_AGENT_TYPE, DONE, UNKNOWN.
- CRITICAL start-path split (review finding): `POST /api/agents/:id/start` ->
  `ensure_agent_started` NO-OPS on a lingering tmux session, which DONE (and
  REPLACED) agents have by definition (`find.py:362-377`, `host.py:3932-3938`)
  -- only the MESSAGE path branches DONE -> `revive_done_agent`
  (`api/message.py:214-222`). A resume click bound to the start endpoint alone
  would spin forever on exactly the process-death case.
- `POST /start` 200 means "spawn issued", NOT "TUI ready"
  (`is_readiness_awaited=False`, `find.py:373-377`). Safe regardless: every
  delivery path independently blocks on `wait_for_tui_ready` before pasting
  (`tui_agent.py:137`), so input in the gap buffers server-side. Do not
  "simplify" the send-side wait away.
- Observe-stream death (`agent_manager.py:936-955`) currently just logs; state
  would freeze forever. The overlay makes `state` load-bearing, so this gets a
  degrade path (below).
- Overlay region: `footer.app-footer` (`ChatPanel.ts:868-903`) MINUS the
  `composer-under-bar-actions` group (terminal/auth exemption above).
  Proto-agents render no footer (out of scope for free). Saved-layout terminal
  tabs start agents themselves (`AgentTerminalPanel.ts:29`).

## Shared liveness predicate (one definition, one file)

`agentLiveness.ts` exports the predicate; the dot and the overlay both consume
it: alive = {RUNNING, WAITING, RUNNING_UNKNOWN_AGENT_TYPE}. REPLACED, DONE,
STOPPED, UNKNOWN are non-alive (REPLACED joins the dormant bucket -- matches
the dot today; a resume must revive it exactly like DONE anyway).

## Overlay state machine

```
agent absent from snapshot / WS not yet connected -> NO overlay (never flash
                                                     before data exists)
alive                                             -> no overlay
STOPPED | DONE | REPLACED                         -> overlay: "Agent stopped" + [Resume agent] button
UNKNOWN                                           -> overlay: "Agent unavailable" + [Retry] button
pending resume                                    -> overlay: "Starting agent..." (spinner)
resume failed                                     -> overlay: "Couldn't start" + detail + [Retry]
```

- Mechanics of "covered": the footer stays MOUNTED with the `inert` attribute
  set (blocks pointer AND keyboard/focus; blur any focused element inside on
  raise) -- a visual cover alone is bypassable from the keyboard (review
  finding: a user typing when the agent dies still has focus; Enter would
  send). Composer draft and staged attachments survive (localStorage +
  module-keyed state, verified).
- The resume affordance is a DISCRETE button inside the overlay, not a
  whole-footer click target (misclick/scroll-overshoot must not boot
  processes; also the mobile thumb zone).
- `pending resume` is frontend-local per-agent state: set on click, cleared by
  an alive push (authoritative). After the POST resolves 200, a non-alive push
  arriving past a short grace (~10s) flips to the failed state (start
  succeeded then crashed). POST error -> failed state immediately. ~90s
  failsafe backstops a wedged start.
- Restart-transient suppression (review finding): the stop/interrupt button
  and shoulder-tap SIGKILL-then-relaunch the agent; the STOPPED push in that
  window must not flash the overlay under the user's cursor. Suppress the
  stopped overlay for a short window (~15s) after any UI-initiated
  restart/flush for that agent (same class of fix as QueuedMessageView's
  anti-blip freeze, `QueuedMessageView.ts:7-11`).
- External transitions: another surface starting the agent clears via the
  push; external `mngr stop` raises via the push. An internal auto-start
  (welcome resend, cross-agent send) may briefly stream transcript under a
  raised overlay until its alive push lands -- accepted, self-heals in one
  push.
- Drag-and-drop / paste-to-attach keeps working while the overlay is up
  (attachments are draft state, like composer text); overlay copy should not
  imply the composer is dead.
- The `conversation-before-input` EmptySlot sits under the overlay (inert with
  the rest); plugin UI there is composing-adjacent.

## Backend changes (revised by review -- NO 409 belt)

1. Fix the start endpoint's lifecycle blind spot: `POST /api/agents/:id/start`
   mirrors the message path -- probe lifecycle; DONE/REPLACED ->
   `revive_done_agent`, else `ensure_agent_started`. (Also corrects the
   endpoint docstring's "same path as messaging" claim, which is false today
   for DONE.)
2. Observe-death degrade: when the observe watchdog detects stream exit,
   degrade all tracked agents' state to UNKNOWN and broadcast (the overlay's
   UNKNOWN surface is the honest rendering of "we cannot know"), and/or
   restart observe. Never leave RUNNING/STOPPED frozen.
3. NO 409 guards (dropped entirely -- both reviewers converged): the model
   endpoint must stay reachable because the fast-mode prompt modal legitimately
   fires on a stopped agent and relies on auto-start (`WorkspaceFastMode.ts:80-90`);
   the flush endpoint is itself a restart-based recovery. The contract is
   enforced by the UI gate (`inert` footer), not by the API.

## Considered and declined

- Read-only chips + inline notice instead of an overlay (reviewer 2's simpler
  composite): declined by explicit user decision -- the three-state overlay IS
  the requested contract -- but its good parts are folded (discrete button,
  draft-preserving copy, reuse of `AgentTerminalPanel`'s start-spinner
  pattern).
- Auto-start on tab visit: rejected; browsing must not boot processes.
- Interrupt-button redesign, pi consume-by-rename: separate follow-ups.

## Verification (build time)

- Unit: derivation table (each lifecycle value x pending x suppression x
  no-snapshot).
- Manual, desktop AND mobile layout:
  - `mngr stop` a scratch agent -> stopped overlay; transcript scrolls; chip
    shows last-known model; drafts/attachments preserved; Tab cannot reach
    covered controls; Resume -> spinner -> unlock on push.
  - Kill the harness process inside its pane (DONE) -> overlay -> Resume
    actually revives (the F1 case; must go through revive_done_agent).
  - Interrupt and shoulder-tap on a RUNNING agent -> NO overlay flash.
  - Terminal + auth buttons work while the overlay is up.
  - Stop observe (kill the subprocess) -> agents degrade to UNKNOWN, overlay
    says unavailable, recovers when observe returns.

## Build mechanics (when approved)

system_interface worker pass (update-system-interface flow), self-verified
preview against stopped AND killed (DONE) agents, merge + reveal. The two
backend changes ride the same branch. One worker pass, medium-small.
