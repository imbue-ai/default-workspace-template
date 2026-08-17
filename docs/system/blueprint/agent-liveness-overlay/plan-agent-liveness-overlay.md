# Plan: agent liveness overlay (the "if you can interact, it's live" contract)

Status: v5 -- explored, adversarially reviewed (2 passes), user feedback folded
(light capsule UI, per-element inert, native interrupt vetoed, pending-restart
flash protection). Mocks: `data/images/liveness-capsule-*.png`. Pending user go.

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
agent absent from snapshot / WS not yet connected -> NO capsule (never flash
                                                     before data exists)
alive                                             -> no capsule
STOPPED | DONE | REPLACED                         -> capsule: "Agent stopped" + [Resume] button
UNKNOWN                                           -> capsule: "Agent unavailable" + [Retry] button
pending resume OR pending restart                 -> capsule: "Starting agent..." (spinner)
resume failed                                     -> capsule: "Couldn't start" + detail + [Retry]
```

`pending restart` is the flash protection -- see below.

- UI treatment (user direction: nice, compact, not dark): NO full-region
  scrim. The gated elements (`MessageInput`, `ModelBar`, the
  `conversation-before-input` slot) get the `inert` attribute plus a light
  dim (reduced opacity, no dark backdrop), and a COMPACT centered capsule
  floats over the input area carrying the state -- "Agent stopped [Resume]" /
  "Starting agent..." spinner / "Couldn't start [Retry]" + detail. The
  capsule is the only new visual chrome; everything else keeps its normal
  look. Match the app's existing pill/button styling; both themes.
- Exemption mechanics: `inert` is PER-ELEMENT, so there is no overlay
  geometry problem -- the `composer-under-bar-actions` group ("Open agent
  terminal", "Agent auth") simply never gets `inert` or the dim, stays fully
  live in the same row. No polygon, no cutouts.
- Why `inert` and not just a visual cover: a cover alone is bypassable from
  the keyboard (a user typing when the agent dies still has focus; Enter
  would send). `inert` blocks pointer AND keyboard/focus; blur any focused
  element inside on raise. Composer draft and staged attachments survive
  (localStorage + module-keyed state, verified).
- `pending resume` is frontend-local per-agent state: set on click, cleared by
  an alive push (authoritative). After the POST resolves 200, a non-alive push
  arriving past a short grace (~10s) flips to the failed state (start
  succeeded then crashed). POST error -> failed state immediately. ~90s
  failsafe backstops a wedged start.
- Flash protection (`pending restart` -- needed because the interrupt stays
  restart-based; the native-interrupt idea is DEAD, user veto). Mechanism:
  relabel, don't hide. When the UI itself initiates a restart -- the
  interrupt/stop button or the shoulder-tap, both of which SIGKILL-then-
  relaunch (`server.py:708-725`) -- the frontend marks that agent
  `pending restart` locally at the moment of the POST. While the mark is set,
  a non-alive push renders the "Starting agent..." spinner capsule instead of
  the "Agent stopped [Resume]" capsule -- the composer is correctly gated
  (the agent really is down for a moment) but the user sees restart progress,
  not a scary stopped state with a Resume button racing the in-flight
  restart. The mark clears on the next alive push (authoritative), or falls
  through to the stopped capsule after a failsafe (~30s) if the restart never
  lands. So: no flash of "stopped", no suppression window hiding truth, no
  double-start race (the spinner capsule has no button). This is the same
  class of fix as QueuedMessageView's anti-blip freeze
  (`QueuedMessageView.ts:7-11`), applied to the capsule.
- New-chat creation never shows the capsule at all: the proto-agent phase
  renders no footer (`ChatPanel.ts:865`), and the agent joins the store
  already RUNNING (`agent_manager.py:766`).
- External transitions: another surface starting the agent clears via the
  push; external `mngr stop` raises via the push. An internal auto-start
  (welcome resend, cross-agent send) may briefly stream transcript under a
  raised capsule until its alive push lands -- accepted, self-heals in one
  push.
- Drag-and-drop / paste-to-attach keeps working while the capsule is up
  (attachments are draft state, like composer text); the copy should not
  imply the composer is dead.

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
- Native in-TUI interrupt (Esc keypress instead of restart): VETOED by the
  user -- will not work out; do not build. Interrupt stays restart-based,
  which is why the `pending restart` flash protection above exists.
- pi consume-by-rename: separate follow-up.

## Verification (build time)

- Unit: derivation table (each lifecycle value x pending x no-snapshot).
- Manual, desktop AND mobile layout:
  - `mngr stop` a scratch agent -> capsule appears; transcript scrolls; chip
    shows last-known model; drafts/attachments preserved; Tab cannot reach
    inert controls; terminal/auth stay live and undimmed; Resume -> spinner ->
    unlock on push.
  - Kill the harness process inside its pane (DONE) -> capsule -> Resume
    actually revives (the F1 case; must go through revive_done_agent).
  - Interrupt and shoulder-tap on a RUNNING agent -> the "Starting agent..."
    spinner capsule shows during the restart window (never the stopped
    capsule, never a Resume button), then clears on the alive push.
  - Stop observe (kill the subprocess) -> agents degrade to UNKNOWN, capsule
    says unavailable, recovers when observe returns.
  - Visual: capsule is compact, light (no dark scrim), matches both themes.

## Build mechanics (when approved)

system_interface worker pass (update-system-interface flow), self-verified
preview against stopped AND killed (DONE) agents, merge + reveal. The two
backend changes ride the same branch. One worker pass, medium-small.
