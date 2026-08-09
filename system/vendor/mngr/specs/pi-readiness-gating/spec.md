# Spec: gate an agent's "ready" surface on its UI signals (fix pi startup flicker)

## Problem

When a `pi-coding` chat starts, the workspace UI shows a broken half-started
state for several seconds: the status dot is wrong (grey / not-yet-live), the
model bar is blank, and a message sent in that window takes an unreliable 5-15s.
It self-corrects later. `claude` and `codex` chats do not show this.

## Root cause (measured)

A pi agent writes the files the UI reads **~7.7s after its process starts**:

| signal file (per agent state dir)        | claude       | codex           | pi            |
|------------------------------------------|--------------|-----------------|---------------|
| session/transcript (drives activity dot) | at launch    | at launch       | **+7.7s**     |
| `minds_model_state.json` (model bar)     | at launch    | ~0.5s           | **+7.7s**     |

Reproduced twice: pi's `pi_process_started` marker is touched at exec, but
`pi_session_file` and `minds_model_state.json` are only written when pi's
lifecycle extension's `session_start` handler fires — which happens after pi's
engine boots and its (1153-option) model registry resolves. claude/codex emit
their session + model-state natively at process launch, so their gap is
negligible.

Nothing makes the UI wait for pi to finish that self-initialization:

- The chat-create path (`_build_chat_create_command` in the system interface)
  creates the agent with `--no-connect` and **no initial message**. In
  `api/create.py` that takes the `else` branch (`just start the process`) and
  **never calls `wait_for_ready_signal`**. So `mngr create` returns, and the tab
  appears, the instant the process spawns — ~7.7s before pi can be used.
- The live status the dot/model bar render comes from the observe stream +
  the on-disk signal files, not from any readiness gate. During pi's boot the
  lifecycle probe cannot yet confirm the `pi` process, so it reads as not-live
  (grey), then flips to RUNNING; the model bar stays blank until
  `minds_model_state.json` lands.

pi already emits a readiness sentinel — `pi_session_started`, written by the
extension on `session_start`, waited on by `PiAgent.wait_for_ready_signal`. It
is simply never consulted for a no-initial-message create, and (see below) it
can fire slightly before the model resolves.

## Goal

An agent should surface to the user as **ready/live only once it has published
the signals the chat UI depends on** — its transcript is open and its model is
known. Until then the UI shows an explicit, calm "starting" state rather than a
wrong dot + blank model bar. Readiness is a mngr/minds construct we already
own, so we define it and make the UI honor it. `claude`/`codex` behavior is
unchanged.

## Definition of "ready"

An agent is **ready** when both hold:

1. its session/transcript exists (activity + transcript are derivable), and
2. `minds_model_state.json` exists and is non-empty (model bar can render).

Readiness is **opt-in per harness**, declared by a readiness-marker path on the
agent plugin. A harness that declares no marker is treated as *ready whenever it
is RUNNING/WAITING* — i.e. exactly today's behavior. So this change is inert for
claude and codex unless they later opt in.

For pi the readiness marker is the existing `pi_session_started` sentinel,
tightened (see change 4) so it is only written **after** `minds_model_state.json`
has been written — making "sentinel present" ⟺ "session open AND model known".

## Design

Propagate a boolean `is_ready` from the plugin through the observe stream to the
frontend, and render a "starting" treatment while an alive agent is not yet
ready. A boolean, not a new lifecycle state: readiness is orthogonal to liveness
(a RUNNING agent can be not-yet-ready) and a new `AgentLifecycleState` member
would touch every state consumer, the observer's sticky-UNKNOWN logic, and their
tests for no added value.

Because `is_ready` is re-derived from the live marker on every observe snapshot,
it self-heals for **both** fresh-create and workspace-restart/revive (where pi
re-runs its ~7.7s boot and no create call happens) — no dependency on the create
path blocking.

### Changes

**1. Plugin declares a readiness marker + emits `is_ready` (mngr, pi plugin).**
- Add an optional readiness-marker path to the agent plugin surface. `PiAgent`
  returns `<state_dir>/pi_session_started`; claude/codex return `None`.
- Emit `is_ready` via the existing `agent_field_generators` hook (the same
  mechanism as `waiting_reason`), computed as: no marker declared → `True`;
  marker declared → marker file exists. Keep it cheap: a single `stat`, on the
  path mngr already knows.

**2. Carry `is_ready` on `AgentDetails` + observe (mngr).**
- Add `is_ready: bool = True` to `AgentDetails` (default keeps every existing
  producer/consumer correct). Populate it from the field generator.
- Include it in the observe `AGENT_STATE` / `AGENTS_FULL_STATE` payloads so the
  system interface sees it change live.

**3. Surface `is_ready` in the system interface (dwt, system_interface).**
- Add `is_ready: bool` to `AgentStateItem`, thread it through
  `_handle_observe_event` / `_refresh_agents` / `discover_agents`, and include it
  in `get_agents_serialized()` and the initial WS snapshot.
- Frontend: when `state ∈ {RUNNING, WAITING}` and `is_ready == false`, render a
  **starting** treatment:
  - status dot: a distinct "starting" style (e.g. pulsing neutral/blue), NOT the
    grey dormant style and NOT green — see `agentLiveness.ts`.
  - model bar: a "starting…" placeholder instead of the blank/logo-only state.
  - composer: optional "starting…" hint; sending stays enabled (an early send
    lands in `pi_inbox` and is injected on ready — it is not lost).
  Once `is_ready` flips true, the existing green/model rendering takes over.

**4. Make pi's sentinel imply model-ready (mngr, `mngr_pi_lifecycle.ts`).**
- Today `session_start` writes `pi_session_started` unconditionally but skips
  `minds_model_state.json` when the model has not resolved (`if (!provider ||
  !modelId) return`). Reorder so the sentinel is written **after** a successful
  model-state write. If the model cannot resolve within a bounded window, write
  the sentinel anyway (agent must not be stuck "starting" forever) and let the
  model bar show an "unknown model" placeholder — availability wins over a
  perfect bar.

### Optional, not required

- Do **not** make chat-create block on the sentinel as the primary fix: it would
  just move the 7.7s into `mngr create` (the tab wouldn't appear at all for that
  time — worse perceived latency) and would do nothing for the restart/revive
  path. The `is_ready` surface is strictly better. A blocking create could later
  be offered behind an explicit flag, out of scope here.

## Edge cases

- **Harness without a marker** (claude/codex): `is_ready` defaults True; UI
  identical to today.
- **Remote / UNKNOWN provider**: an agent in STOPPED/DONE/UNKNOWN is rendered by
  liveness as before; `is_ready` only modifies the RUNNING/WAITING rendering.
- **Marker present but stale** (dead generation left it): the process is not
  RUNNING in that case, so the `state ∈ {RUNNING, WAITING}` guard means the
  stale marker never forces a false "ready".
- **Model never resolves**: bounded fallback in change 4 flips ready with an
  "unknown model" bar rather than hanging in "starting".
- **Send while starting**: preserved via `pi_inbox`; the UI just labels it
  honestly instead of showing a misleading state.

## Testing

- mngr unit: `is_ready` field generator — marker absent → false, present → true,
  no-marker harness → true.
- mngr TS unit (`mngr_pi_lifecycle_test.py`): `pi_session_started` is written
  only after `minds_model_state.json`; bounded fallback still writes the sentinel
  when the model never resolves.
- system_interface unit: a not-ready observe event serializes `is_ready=false`;
  the frontend maps `RUNNING && !is_ready` to the starting treatment
  (`agentLiveness` test).
- acceptance (mngr, local provider): create a pi agent; assert `is_ready`
  transitions false→true and that `minds_model_state.json` exists exactly when it
  is true.

## Rollout

Mechanical, back-compatible (all new fields default to the current behavior).
Ship mngr changes (1, 2, 4) and the system_interface changes (3) together so the
UI has the field to read; if they land separately, the UI simply sees
`is_ready` default true (today's behavior) until mngr ships. Changelog entries
in both `libs/mngr` and (dwt) `system_interface`.

## Out of scope

- Reducing pi's ~7.7s boot itself (model-registry enumeration). Worth a separate
  investigation; this spec makes the boot honest, not faster.
- Any change to claude/codex readiness.
