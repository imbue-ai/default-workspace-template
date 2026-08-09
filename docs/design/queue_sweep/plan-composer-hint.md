# Composer placeholder hint: mid-turn typing queues, it does not interrupt

## Contract being enforced

None of A/B/C directly -- this is their user-facing legend. Contract A makes mid-turn sends
park in a real, mirrored queue; nothing in the composer says so, and users type expecting an
interrupt. While the agent has a turn in flight, the placeholder should read
"Type to queue more messages..." so the queueing semantics are taught at the exact moment
they apply. Idle, stopped, and dead agents keep the base wording.

## Today's behavior and what is wrong

- The placeholder is the single static string "Type a message..."
  (`system/apps/system_interface/frontend/src/views/MessageInput.ts:440`), regardless of
  agent state.
- The working predicate already exists three hundred lines into the same `view()`:
  `isAgentWorking = isWorkingActivityState(getAgentById(agentId)?.activity_state ?? null)`
  (`MessageInput.ts:404-408`), currently consumed only by the stop button (`:476`).
  `isWorkingActivityState` maps THINKING / TOOL_RUNNING to true, IDLE / null (untracked or
  proto agents) to false (`ActivityIndicator.ts:47,53-55`).
- The signal already reaches the frontend with no backend change: `activity_state` rides
  the `agents_updated` WS payload into the model (`models/AgentManager.ts:31,277-291`), and
  each accepted push redraws -- the stop button appears and disappears through exactly this
  path today, so the placeholder inherits the same freshness for free.

Nothing here is wrong so much as missing: one string where two are needed.

## Minimal change

Tear out the static `placeholder: "Type a message..."` (`MessageInput.ts:440`); replace with
a ternary on the existing predicate:

    placeholder: isAgentWorking ? "Type to queue more messages..." : "Type a message...",

Decisions, all resolved toward the smaller diff:

- Predicate is `isAgentWorking`, not `isStopButtonVisible`: `isInterruptInFlight` (`:408`)
  is a client-side double-click debounce, and coupling copy to it buys nothing real. To be
  honest about the interrupt window: a send after `_drain_queue` captures the block
  (`server.py:761`) but before the SIGKILL lands (`:764`) parks in the harness queue and is
  destroyed -- a pre-existing hazard of the restart path that neither wording changes
  (`isStopButtonVisible` would flip the hint off at the click, at the cost of reading
  component-local debounce state). The wording self-corrects when the backend broadcasts
  IDLE (`reset_activity_state`, `server.py:706,770`).
- Queued-bubbles-present changes nothing: the "Queued messages" header
  (`QueuedMessageView.ts:121`) already labels the parked bubbles; a second variant string
  and a queue-snapshot read here would buy no extra learning. One wording whenever working.
- Stopped/dead agents need no branch here, but the guarantee is per-harness today: claude
  and pi gate derive on the lifecycle (`harnesses/claude/activity_state.py:43-44`,
  `harnesses/pi_coding/activity.py:38-45`), so dead -> IDLE/null -> base wording; codex
  deliberately ignores `is_agent_running` (`harnesses/codex/activity.py:50-53`), so a codex
  agent killed mid-turn broadcasts THINKING indefinitely and would show the queue wording.
  plan-codex-queue move 2 (dead lifecycle forces IDLE in `_recompute_activity_state`) closes
  exactly that hole on the same branch; this plan depends on it landing, not on new code.
- Copy style: three-period ellipsis, matching the sibling string being replaced (ModelBar's
  "Search models…" uses the character, but the in-file precedent wins).

No new model reads, no new signal, no CSS, no backend change.

## Ship mechanics

One file plus its test, in `system/apps/system_interface/frontend/`; lands on
`claude-codex-pi-dwt`. No mngr change, no codex-fork rebuild. Ships with the normal
frontend build (`npm run build`).

## Tests

In the existing `frontend/src/views/MessageInput.test.ts`: DELETE the
`vi.mock("./ActivityIndicator", ...)` pin (`:67`) -- the real module is safe to load under
the existing mocks (mithril, type-only imports, and the already-mocked
`../models/AgentManager`), and `isWorkingActivityState` is a pure function worth pinning
for real. The flip point already exists: `getAgentById` returns the mutable `mocks.agent`
holder (`:34,:65`), which tests already flip per-test (`:189,:196`); add an
`activity_state` field (undefined by default -- keeps all existing tests byte-identical --
reset in `beforeEach`). Then:

- default (undefined activity_state) -> textarea placeholder is "Type a message...".
- `mocks.agent.activity_state = "THINKING"` -> "Type to queue more messages...".

Run: `npm test` in `frontend/` (vitest).

## Open risks

- The hint's honesty depends on `activity_state` being current, in both directions. Stale
  THINKING (tracker lag after a crash; for a dead codex agent, a persistent state until
  plan-codex-queue move 2 lands -- see Minimal change) shows the queue wording while a send
  would start a turn. Symmetrically, stale IDLE: activity flips to THINKING only after the
  sent message reaches the transcript and a watcher poll recomputes (the send endpoint,
  `server.py:405-450`, triggers no recompute), so for about one poll interval after any
  send a second message would queue under the base wording. Both are pre-existing scope of
  the activity tracker (the stop button shows the same lies); the Contract A plans tighten
  the signal.
- During a restart-based stop or flush, `reset_activity_state` broadcasts IDLE
  mid-operation (`server.py:706,770`) while the queued group above may still read "Sending
  queued messages..." under the flush freeze (`QueuedMessageView.ts:50-54,100-111`) -- a
  brief cosmetic contradiction, then the resent block flips the wording back. Accepted; do
  not "fix" it by reading the freeze state (that adds the queue-snapshot coupling this plan
  rejects).

## Non-goals

- Queue rendering, the stop button, and the shoulder tap (sibling plans).
- Any per-harness variation: the wording is harness-agnostic because the queueing behavior
  the contracts enforce is.
- Placeholder changes for proto-agents or any other composer copy.
