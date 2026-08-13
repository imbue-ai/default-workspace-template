# pi interrupt: stop button becomes a native interrupt-and-retract (no SIGKILL-restart)

Status: SHIPPED -- landed on `claude-codex-pi-dwt` and `claude-codex-pi-mngr` (`793079af`,
mirror-refresh follow-up `9297b430`, merged `bf41fad5`). The accepted in-flight-send race was
later closed by `plan-pi-interrupt-inflight-lock.md`.

## Contract being enforced

Contract B: the stop button interrupts the running turn, hands queued messages back to the
composer, and leaves the harness's own queue empty. The contract is mechanism-agnostic -- the
existing SIGKILL-restart path already satisfies all three clauses for pi (apart from the
empty-queue no-op below). This plan replaces it with the retract sibling of the shipped
native flush anyway, and owns the reasons: no SIGKILL mid-tool-call, no session-resume cost
or 60s restart timeout, no `reset_activity_state` patch-up of an abandoned transcript, and
symmetry with the codex sibling's native retract branch. It also fixes latent defects the
restart path masks (below).

## Today's behavior and what is wrong

- The stop button (`frontend/src/views/MessageInput.ts:238-271`, visible only while working,
  `:404-408`) calls `/drain-to-composer` (`imbue/system_interface/server.py:888-909`): for
  EVERY harness, `_drain_queue` (`server.py:749-772`) captures the mirror block, runs
  `mngr start --restart --no-resume` (SIGKILL + relaunch, `:711-728`), `reset_activity_state`
  + `clear_queue`. It returns early on an empty block (`:761-763`), so the stop button
  silently no-ops on a running turn with nothing queued -- for every harness, contradicting
  its tooltip. (For flush the early return is correct: nothing to resend.)
- The handback is dropped when the composer holds a draft or attachments
  (`MessageInput.ts:250-258`, `isComposerEmpty` guard) -- today the messages at least survive
  in the durable inbox; under a native retract they would survive nowhere. Must be fixed here.
- pi's native interrupt half is shipped: `{"minds_interrupt": true}` appended to `pi_inbox`
  (`server.py:850-864`) triggers `beginShoulderTap()` (`mngr_pi_lifecycle.ts:454-466`): clear
  draft, `ctx.abort()` (drains parked steers into the editor), read back, restore draft,
  resubmit once idle (`pendingResubmit` gate, `:472-482`). Retract is the same dance minus
  the resubmit; only the restart path exists for it today.
- Durable-replay imbalance (a Contract A defect this plan must not widen): the pi mirror
  replays `pi_inbox` from offset 0 (`harnesses/pi_coding/watcher.py:297-309`), skipping
  object lines (`:306-307`), and pops one FIFO head per session `user_message` (`:325-327`).
  The shipped flush already unbalances it: k parked steers resubmitted as ONE merged steer
  yield one `user_message`, leaving k-1 phantoms on a backend restart's replay. A retract
  (discard, no `user_message` at all) would make every retracted line a phantom -- and within
  the CURRENT process generation, where the Contract A sibling's process-start discriminator
  can never filter them. The sentinel, durably parked at exactly the right inbox position, is
  the repair (piece 3 below).

## Minimal change

Tear out: the pi leg of the restart path -- pi requests to `/drain-to-composer` no longer call
`_drain_queue` / `_restart_agent_process` / `reset_activity_state`. Replace with four pieces:

1. Extension (`mngr_pi_lifecycle.ts`): a second sentinel key,
   `RETRACT_KEY = "minds_interrupt_retract"` (same object-line shape and `=== true` check as
   `INTERRUPT_KEY`, `:195,:503-513`). A separate key, not a field on the existing one: an old
   extension would treat `{"minds_interrupt": true, "retract": true}` as a flush and resubmit
   messages Minds just handed back (double-send); an unknown key is inert under skew. Do NOT
   duplicate the dance: extract `beginShoulderTap()`'s abort-and-capture core (idle check,
   clear draft, `ctx.abort()`, read steers, restore draft) into one shared helper; the two
   sentinel branches differ only in the steers' fate (`pendingResubmit = steers` vs discard).
   Drain-loop ordering rule for both keys: `injectSteer` initiates an ASYNC send (`:432-443`),
   so never consume a sentinel in a tick that already injected a string line -- return WITHOUT
   advancing `processedInbox` and process it next tick, so the steer is parked (and thus
   retractable) before the abort.
2. Server (`server.py`): `_drain_to_composer_endpoint` stops branching on harness enums and
   instead dispatches through a per-harness interrupt-to-composer implementation registered
   on the harness (the `switch()` precedent: each `harnesses/<h>/model.py` registers its own;
   backend-only, NO wire-visible catalog flag -- the frontend keeps one button, one endpoint).
   The BASE implementation is the shared restart-drain (today's `_drain_queue` +
   `_restart_agent_process`), used by claude and any future harness by default; pi registers
   the native override: capture `block = watcher.get_queued_block()` FIRST, append the retract
   sentinel to `pi_inbox` (extract the flush endpoint's append at `:855-863` into a shared
   helper), then `watcher.clear_queue()`, return the block. Also hoist the empty-block
   short-circuit out of `_drain_queue` into `_flush_queue_endpoint` only, so claude/codex
   stops interrupt even with nothing queued (matching the unused `/interrupt` endpoint,
   `:667-708`).
3. Watcher (`harnesses/pi_coding/watcher.py` `_consume_inbox`): consume BOTH sentinel object
   lines as a positional clear of the tracked queue (`PiQueueTracker.clear`) instead of
   skipping them (`:306-307`). Every line before a sentinel was either committed (flush) or
   discarded (retract) by the extension, so clearing at the sentinel's replay position keeps
   the ledger balanced across backend restarts -- repairing the flush phantom defect and
   preventing the retract from resurrecting discarded messages. The endpoint's explicit
   `clear_queue()` stays for immediacy; the replay-side clear makes it durable.
4. Frontend (`MessageInput.ts:250-258`): merge instead of drop -- prepend the returned block
   above a non-empty draft (block, blank line, draft). One guard change; closes the only
   path where retracted messages could be lost outright.

Edge cases, resolved by existing mechanisms:

- Empty queue, agent working: write the sentinel anyway (fixes the no-op stop); the extension
  aborts the bare turn, block is `""`, composer untouched.
- Agent idle (turn ended between capture and delivery): the retract handler no-ops. The
  handback stands ungated -- worst case an already-consumed message comes back as an editable
  draft; a visible duplicate, never a silent double-commit. (Same capture-first race as today.)
- Agent process dead: the sentinel parks inertly (`processedInbox` seeded past pre-launch
  lines at startup, `mngr_pi_lifecycle.ts:426`); the replay-side clear is harmless there.
- Double-click: the frontend `isInterruptInFlight` guard (`MessageInput.ts:239-244`); a
  second sentinel finds the agent idle and no-ops.
- Indicator settle without `reset_activity_state`: the abort fires `agent_end`, removing the
  `active` marker (`mngr_pi_lifecycle.ts:654-658`); mngr then reports RUNNING->WAITING, and
  that observe state change is the recompute trigger (`agent_manager.py:1006-1010`) that
  re-gates via `resolve_is_agent_running` (`activity_state.py:182-196`). Corner in Open risks.

## Ship mechanics

- `mngr_pi_lifecycle.ts` (RETRACT_KEY + shared abort core + sentinel tick-deferral): vendored
  tree `system/vendor/mngr/libs/mngr_pi_coding/` -- lands on the claude-codex-pi-mngr branch,
  with an mngr changelog entry per that repo's convention.
- `server.py`, `harnesses/pi_coding/watcher.py`, `MessageInput.ts`: land on claude-codex-pi-dwt.
- No codex fork change, no rebuild, no sha repin. Running pi agents pick up the new extension
  on their next process start (skew risk below).

## Tests

- `mngr_pi_lifecycle_test.py` (extends the inbox-watcher tests at `:486-585`): retract during
  a turn aborts, discards steers, restores the draft, resubmits nothing; retract while idle
  no-ops and later strings still inject; a pre-existing retract line is never processed; a
  string and a sentinel appended together are handled across two ticks (steer injected first).
- `server_test.py` (beside `:931-957` and `:881`): pi drain-to-composer appends the sentinel,
  returns the block, never restarts; pi empty mirror still appends and returns `""`; claude
  drain-to-composer with an EMPTY queue now restarts (changed behavior pinned -- SUPERSEDED
  for claude by plan-claude-interrupt's chord branch and its dispatch tests; the hoist itself
  stays load-bearing for that plan's delegations); flush with an empty queue still no-ops.
- `harnesses/pi_coding/watcher_test.py`: replaying strings then a sentinel (either key)
  yields an empty tracked queue; strings after the sentinel re-enqueue.
- `frontend/src/views/MessageInput.test.ts`: handback with a non-empty draft prepends the
  block instead of dropping it.

## Open risks

- Capture-vs-consume race: a steer pi consumed just before the tap may still be in the block;
  the handback can include a delivered message. Accepted (draft, user-reviewed; same as today).
- The tick-deferral assumes an initiated `pi.sendUserMessage` lands within one 200ms poll; if
  later still, a retracted-and-handed-back message can also commit -- degraded to the visible
  duplicate class above, not a silent loss. Load-bearing invariant to verify once against
  pi's send internals when implementing.
- Flush-then-stop in quick succession: the retract sentinel is processed only after
  `pendingResubmit` clears; if it lands in the idle gap before the merged turn starts, the
  retract no-ops and the merged turn runs to completion -- the flushed messages are committed
  (they cannot be retracted). The stop's handback block is empty by then (the mirror cleared
  at the flush sentinel), so nothing is double-delivered. Accepted.
- Indicator-settle corner: for a turn so short observe never reported RUNNING (the WAITING
  tie-break, `activity_state.py:174-178`), an abort that writes no session event leaves the
  indicator pinned until the next transcript event. A stop-worthy turn has virtually always
  been seen as RUNNING; accepted, no extra recompute machinery.
- Extension version skew: a pi process launched before this lands ignores the retract
  sentinel: the turn keeps running while Minds cleared its mirror and handed back the block.
  Rare, self-healing on the next agent restart; no handshake.

## Non-goals

- Contract A (process-session scoping) and Contract C for pi (flush is shipped) -- sibling
  plans. The watcher sentinel-clear here removes the retract/flush replay imbalance so the
  sibling's process-generation discriminator does not have to.
- The codex and claude native stop paths (`_drain_queue` stays for both; codex has its own
  plan) -- only the empty-queue short-circuit placement changes here.
- Changing `/flush-queue` semantics or the `/interrupt` endpoint (no frontend caller).
