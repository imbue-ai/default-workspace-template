# codex stop button: close the in-flight-send race (SHIPPED, dwt-only, no rebuild)

Follow-up to `plan-codex-interrupt.md`. That plan shipped codex's native interrupt-and-retract
(the `{"retract_turn_id": "<id>"}` line on `shoulder_tap_atomic.jsonl`). It named, and accepted,
the in-flight race in its Open risks. This plan closed it — **dwt-only, no fork rebuild, no spec
change.** Landed on `claude-codex-pi-dwt`.

An earlier draft of this plan proposed a codex-binary "input-drain fence" plus a semantic-spec
update. Both were **withdrawn** after review: codex's send is STRICT and holds `message.lock`
until the steer has actually PARKED (the queued-input sidecar record is written synchronously
with the park), so the dwt lock alone provably closes the race. See "Why no rebuild" below.

## Contract being enforced

Contract B, sharpened: Stop returns to the composer every message queued *or in flight* when it
was pressed, and leaves nothing running.

## The race

Two concurrent Flask requests on one agent:

- `POST /message` → `send_message_to_agent` → `CodexAgent` (an `InteractiveTuiAgent`) pastes the
  steer into codex's tmux pane (`base_agent.send_message`, under `self._message_lock()`,
  send-keys + Enter). "sending…" spans this.
- `POST /drain-to-composer` → `CodexInterruptToComposer.drain_to_composer`: it previously
  captured the block, computed `current_open_turn_id`, appended `{"retract_turn_id": T}`, and
  cleared the mirror — **taking no lock.**

Without the lock the retract line could be appended before codex parked the pasted steer. If the
paste lands after codex aborts turn T, codex is idle and the steer opens a **new turn** (the ABA
gate does not help — the retract was gated on T, the new turn has a fresh id): **the message runs
anyway.** Or, ordered the other way, it is discarded but missing from the block: **lost.**

## What shipped

**dwt — bounded lock + hammer fallback** (`harnesses/codex/model.py`), identical in shape to the
pi sibling. The retract is taken under the SAME `message.lock` mngr's send holds, via
`try_hold_message_lock` (`harnesses/interrupt.py`):

1. Acquire `message.lock` with a bounded, non-blocking wait (`STOP_LOCK_WAIT_SECONDS = 2.0`).
2. **Acquired**: refresh (`get_all_events`), capture the block, and write `retract_turn_id`
   **under the lock**. Because codex's send is STRICT (below), acquiring the lock means the
   in-flight steer is already parked in `pending_steers` and present in the captured block, so
   the retract discards it and it reaches the composer.
3. **Still held past the wait** (an idle-start send holding the lock through its turn-confirm):
   fall back to the base restart-drain. The SIGKILL boundary stops the turn and the in-flight
   message dies with the process — never runs. The common stop keeps the gentle native path.

No fork change, no `shoulder_tap_atomic.jsonl` format change, no `minds_model_state`/ledger touch.

## Why no rebuild (the withdrawn fence)

codex's mid-turn send is STRICT: `InteractiveTuiAgent.send_message` (`tui_agent.py`) holds
`message.lock` across `submit_message_and_confirm` and only releases once the queued-input
sidecar advances (`mngr_codex/plugin.py:433-469`), and the fork writes that sidecar record
*synchronously with the park* (`append_queued_input` immediately before `pending_steers.push_back`,
same serial tick, no await). So by the time the drain acquires the lock and appends the retract
line, the steer is already parked and the existing `on_interrupted_turn` discard drops it. The
proposed binary "input-drain fence" protected a window the lock already covers; it was also not
cleanly implementable (`event::poll(ZERO)` inside the app-event handler cannot reach the
crossterm reader's events) and its build-gate test would have passed without it. The spec edit
put a cross-process observer obligation into a doc scoped to binary behavior — a category error.
All three withdrawn.

## Files / tests

- `harnesses/interrupt.py` — shared `try_hold_message_lock`; `harnesses/interrupt_test.py`.
- `harnesses/codex/model.py` — the lock gate.
- `server_test.py` — native-path drain tests unchanged (lock uncontended), plus a new test
  asserting fallback-to-restart when a send holds the lock (no control line written).

## Residual / accepted

- **Slow-send corner.** Stop pressed inside a send that stays in flight past the 2s wait (a
  laggy/remote host) hammers: that message is stopped, not recovered to the composer — never runs.
- **Lock-path identity** (verification item): the fix relies on `agent_state_dir/message.lock`
  being the file mngr flocks for a codex agent. codex state lives under `plugin/codex/home` but
  the lock is at the state-dir root, consistent on both sides; claude relies on the same identity.

## Non-goals

- pi (its own plan) and claude (empty-queue chord already locks). Any change to Flush semantics,
  the section-4 ledger, or `/interrupt`.
