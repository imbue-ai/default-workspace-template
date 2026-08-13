# pi stop button: close the in-flight-send race (SHIPPED)

Follow-up to `plan-pi-interrupt.md`. That plan shipped pi's native stop (interrupt-and-retract
via a `pi_inbox` sentinel). It left one window open, which it named in its Open risks: a message
still *in flight* (a `POST /message` mid-append, "sending…") when Stop fires could escape the
handback. This plan closed it. dwt harness override + one pi-extension edit; **no rebuild.**

Landed on `claude-codex-pi-dwt` (harness override, lock helper, tests) and
`claude-codex-pi-mngr` (the extension settle-gate).

## Contract being enforced

Contract B, sharpened: Stop returns to the composer **every** message that was queued *or in
flight* at the moment the user pressed it, and leaves nothing running.

## The race

Two concurrent Flask requests on one agent:

- `POST /message` (`server.py:417`) → `agent_manager.send_message_to_agent` → pi's
  `send_message` (`mngr_pi_coding/plugin.py:533`), which holds `self._message_lock()` (flock on
  `<agent_dir>/message.lock`) and appends the message as one JSON *string* line to `pi_inbox`.
  The whole `POST` is the "sending…" window. (A mid-turn steer confirms fast: `_confirm_turn_started`
  returns immediately when the `active` marker is already up, so the lock is held only for the
  append. Only an idle-start send polls up to `_TURN_CONFIRM_TIMEOUT_SECONDS = 16.0`.)
- `POST /drain-to-composer` (`server.py:929`) → `PiInterruptToComposer.drain_to_composer`: it
  previously captured the block, appended the retract sentinel, cleared the mirror — **taking no
  lock.**

`pi_inbox` is one ordered append-only file the extension drains in order. Two unsynchronized
appenders (the in-flight message S and the retract sentinel) let the file order be a coin flip:
sentinel-before-S ⇒ the extension aborts+discards at the sentinel, then injects S as a fresh
**new turn** (and the block, captured before S landed, doesn't carry it) — **S runs anyway**;
S-before-sentinel-but-block-missed-it ⇒ **S silently lost**. On top of that, even with the
right file order, pi's `injectSteer` is async and the old code gave a just-injected steer only
one ~200ms poll to park before the abort — a slow park could still escape.

## What shipped

**dwt — bounded lock + hammer fallback** (`harnesses/pi_coding/model.py`). The retract is now
taken under the SAME `message.lock` mngr's send holds, via `try_hold_message_lock`
(`harnesses/interrupt.py`, lifted there alongside `agent_message_lock` from `claude/tap.py`):

1. Acquire `message.lock` with a bounded, non-blocking wait (`STOP_LOCK_WAIT_SECONDS = 2.0`).
2. **Acquired** (no send in flight, or one released within the wait): capture the block and
   append the retract sentinel **under the lock**. The lock guarantees an in-flight message's
   inbox line landed first, so the order is `[…, S, RETRACT]`: the extension injects S as a
   steer, discards it at the sentinel, and S is in the returned block → composer.
3. **Still held past the wait** (an idle-start send holding the lock through its turn-confirm):
   fall back to the base restart-drain. The SIGKILL boundary stops the turn and the in-flight
   message dies with the process — never runs. The common stop keeps the gentle native path.

**mngr — wait-for-park** (`mngr_pi_lifecycle.ts`). The extension now tracks the count of
in-flight `sendUserMessage` injections (incremented on send, decremented in a `finally`) and
defers a sentinel while any remain outstanding, in addition to the existing same-tick guard. So
the abort fires only once every injected steer has actually parked — provably retractable,
regardless of send latency, rather than assumed-parked-within-one-poll.

Together: the dwt lock orders the retract after the in-flight message, and the extension gate
ensures that message is parked before the abort discards it. S reaches the composer and never
runs.

## Files / tests

- `harnesses/interrupt.py` — `try_hold_message_lock` (bounded) + `agent_message_lock` (blocking,
  lifted from `claude/tap.py`); `harnesses/interrupt_test.py`.
- `harnesses/pi_coding/model.py` — the lock gate; `harnesses/claude/tap.py` re-imports the lifted
  helper (no behavior change).
- `harnesses/pi_coding/model_test.py` / `server_test.py` — the native path (lock uncontended) is
  unchanged, plus a new test asserting fallback-to-restart when a send holds the lock (no sentinel
  written).
- `mngr_pi_lifecycle.ts` + `mngr_pi_lifecycle_test.py` — the settle-gate and a slow-park test
  (send resolves after ~500ms) asserting park-then-abort ordering.

## Residual / accepted

- **Slow-send corner.** Stop pressed inside a send that stays in flight past the 2s wait (a
  laggy/remote host, not the local host dwt runs on) hammers: that one message is stopped, not
  recovered to the composer — but never runs. Safety is unconditional; only "recover to composer"
  degrades, in a window that essentially never triggers locally.
- **Lock-path identity** (verification item): the fix relies on `agent_state_dir/message.lock`
  being the exact file mngr flocks for a pi agent. claude already depends on this and pi's own
  `pi_inbox` path resolves from the same root, so it holds — worth a glance only if the fallback
  ever fires spuriously.
- **Remote host.** mngr no-ops `message.lock` for non-local hosts, so the serialization holds
  only locally — the environment the system interface runs in.
- The pre-existing capture-vs-consume duplicate for a steer pi *committed* just before the tap
  (`plan-pi-interrupt.md` Open risks) is a delivered message, not an in-flight one; unchanged.

## Non-goals

- codex (its own plan, `plan-codex-interrupt-inflight-lock.md`). claude's empty-queue chord path
  already locks; its nonempty path uses SIGKILL, where an in-flight message is dropped-not-run.
