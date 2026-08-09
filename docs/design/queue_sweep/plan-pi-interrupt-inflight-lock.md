# pi stop button: close the in-flight-send race by draining under mngr's message lock

Follow-up to `plan-pi-interrupt.md`. That plan shipped pi's native stop
(interrupt-and-retract via a `pi_inbox` sentinel). It left one window open, which it
named in its Open risks: a message still *in flight* (a `POST /message` mid-paste,
"sending…") when Stop fires can escape the handback. This plan closes it, dwt-only,
**no mngr change and no rebuild**.

## REVISED AFTER ADVERSARIAL REVIEW — read this first

An adversarial review confirmed the lock closes the race for the **turn-running** case (the
one the user asked about), but found the naive version below is incomplete and introduces two
regressions. Treat the body below as the starting sketch; the binding changes are here:

1. **Up-to-16s Stop-button hang (must fix).** mngr's pi `send_message` holds `message.lock`
   across BOTH the inbox append AND `_confirm_turn_started`, which polls up to
   `_TURN_CONFIRM_TIMEOUT_SECONDS = 16.0` when the send opened a turn
   (`mngr_pi_coding/plugin.py:167,551,567-583`). `agent_message_lock` uses a blocking
   `flock(LOCK_EX)` with no timeout (`tap.py:146-153`). So a Stop pressed during an in-flight
   send freezes for up to 16s. **The drain must not wait on the send's confirmation** — only on
   the durable append. Fix options, pick one at implementation: (a) mngr change so the pi send
   holds the lock across the append only and confirms *outside* it; (b) drain acquires with a
   bounded non-blocking wait and, on timeout, falls back to the base restart-drain (which
   already interrupts). (a) is cleaner and also helps codex; it is a `claude-codex-pi-mngr`
   change, so this plan is no longer strictly dwt-only if (a) is chosen.

2. **Idle-start silent loss (must handle).** The body's step-2 premise — "S is a line in
   `pi_inbox`, so the block includes it" — is FALSE when the agent was idle and S's send
   *starts a turn*: the extension injects S, pi writes S's `user_message`, and the watcher's
   refresh nets enqueue+leave to absent, so `get_queued_block()` returns `""`
   (`harnesses/pi_coding/watcher.py:302-352,377-380`). The lock makes this MORE likely, because
   it releases only after `_confirm_turn_started` sees the turn start — by which point S has
   already popped. Result: block empty AND the retract discards S → **S silently lost**. This is
   the pre-existing optimistic-handback corner (turn ends between capture and tap) but the lock
   widens it. Needs an explicit decision: either capture the block *before* the confirmation
   completes, or accept+document that an idle-start send is a new turn (not an in-flight queue
   message) and let the base restart-drain own that case.

3. **"S never runs" is overstated.** `injectSteer` is async; the tick-deferral gives S one
   ~200ms poll to park before the abort (`mngr_pi_lifecycle.ts:655-666,738-744`). If parking is
   slower, S commits AND is in the handback — a visible duplicate, exactly the residual the
   predecessor plan already accepts (`plan-pi-interrupt.md:127-129`). The lock does not remove
   it. Downgrade the guarantee from "never runs" to "never *silently* runs; worst case a visible
   duplicate."

4. **Remote host voids the barrier.** mngr's `_message_lock` no-ops when `not host.is_local`
   (`base_agent.py:384-386`); dwt always flocks. Against a remote agent the two never contend.
   Accepted only because the system interface is local-only — state that as the explicit
   precondition, not a comment aside.

5. **Implementation gap.** `PiInterruptToComposer.build` stores only `_inbox_path`; add
   `_state_dir` so `agent_message_lock(self._state_dir)` has its argument (`model.py:288-294`).

6. Confirmed sound: the lock-path identity (dwt and mngr flock the same
   `agent_state_dir/message.lock`) and the "append is inside the lock" barrier. Those were the
   parts most at risk; they hold for local hosts.

## Contract being enforced

Contract B, sharpened: Stop returns to the composer **every** message that was queued
*or in flight* at the moment the user pressed it, and leaves nothing running. Today the
"queued" half holds; the "in flight" half races.

## The race (precise)

Two concurrent Flask requests on the same agent:

- `POST /message` (`server.py:417`) → `note_sent_message` (base no-op — no harness
  overrides it) → `agent_manager.send_message_to_agent`. For pi this reaches
  `PiCodingAgent.send_message` (`mngr_pi_coding/plugin.py:533`), which holds
  `self._message_lock()` (flock on `<agent_dir>/message.lock`) and appends the message
  as one JSON *string* line to `pi_inbox`. The whole `POST` is the "sending…" window.
- `POST /drain-to-composer` (`server.py:929`) → `PiInterruptToComposer.drain_to_composer`
  (`harnesses/pi_coding/model.py:296`): capture `block = watcher.get_queued_block()`,
  `append_pi_inbox_sentinel(inbox, PI_RETRACT_KEY)`, `clear_queue()`, return block.
  **It does not take `message.lock`.**

`pi_inbox` is one ordered, append-only file the extension drains in order
(`mngr_pi_lifecycle.ts:602`). Two unsynchronized appenders (the in-flight message S and
the retract sentinel) mean the file order is a coin flip:

- **Sentinel lands before S** → inbox is `[…, RETRACT, S]`. Extension aborts + discards
  the parked steers at RETRACT, next tick injects S as a fresh **new turn**. And the
  drain's `get_queued_block` (run before S was appended) did not include S, so S is
  *not* in the composer either. **S runs anyway** — the exact feared outcome.
- **S lands before sentinel** → inbox is `[…, S, RETRACT]`. Extension injects S as a
  steer, defers the sentinel one tick (`mngr_pi_lifecycle.ts:628`), discards it. S never
  runs — but whether S reached the composer depends on whether `get_queued_block` saw it
  first, so S can be **silently lost** from the handback.

Both failure modes are the same root cause: the drain does not serialize against an
in-flight send.

## Minimal change (dwt only)

Make pi's drain acquire the **same** `message.lock` mngr's send holds, and capture the
block **under** it — the exact pattern `ClaudeInterruptToComposer` already uses
(`harnesses/claude/tap.py:658`, the under-lock re-check). Reuse the existing
`agent_message_lock(agent_state_dir)` context manager (`tap.py:138`); do not write a
second lock helper.

New `PiInterruptToComposer.drain_to_composer` shape:

1. Acquire `agent_message_lock(self._state_dir)`. Acquiring blocks until any in-flight
   `send_message` has released the flock — i.e. until S's `pi_inbox` append has durably
   completed. (mngr's send holds the lock across its whole append.)
2. `watcher.get_queued_block()` — now S is already a line in `pi_inbox`, so the block
   includes it (pi's `get_queued_block` calls `_refresh`, so no separate refresh needed).
3. `append_pi_inbox_sentinel(self._inbox_path, PI_RETRACT_KEY)` — appended *after* S's
   line, because the lock guaranteed S landed first.
4. `watcher.clear_queue()`; release the lock; return the block.

Why this closes it: under the lock the inbox order is deterministically `[…, S, RETRACT]`
(case 2 above, the safe one) for any send that was in flight when Stop was pressed. The
extension injects S as a steer, the tick-deferral parks it, the retract discards it, and
S is in the returned block → composer. S reaches the composer and never runs.

**The lock must wrap the block capture, not just the append.** Appending the sentinel
under the lock but reading the block before it would still miss S in the handback.

## Ship mechanics

- `harnesses/pi_coding/model.py` (add the lock + reorder), on `claude-codex-pi-dwt`, with
  a dwt changelog entry. Import `agent_message_lock` and `MESSAGE_LOCK_FILENAME` from
  `harnesses/claude/tap.py` — or, cleaner, first lift `agent_message_lock` out of the
  claude tap module into a neutral home (`harnesses/interrupt.py`) since it is now shared
  by two harnesses and is not claude-specific. Prefer the lift; note it as a small
  refactor the reviewer should weigh against a plain import.
- No `mngr_pi_lifecycle.ts` change. No mngr branch. No rebuild. No sha repin.

## Load-bearing assumption to verify before landing

`agent_info.agent_state_dir / "message.lock"` (what dwt flocks) must be the **same path**
`PiCodingAgent._get_agent_dir() / "message.lock"` (what mngr's send flocks). claude relies
on this same identity (`tap.py:143` "same filename, same agent state dir"); confirm it
holds for pi's agent dir before trusting the serialization. If pi's per-agent dir differs
(it also has `plugin/pi_coding/` for auth), the lock file root must match mngr's, not the
config subdir.

## Tests

- `harnesses/pi_coding/model_test.py` (or `server_test.py` beside the existing pi
  drain-to-composer tests): with a fake watcher and a real temp `message.lock`, a drain
  that runs while a second thread holds the lock **blocks until release**, and the block
  captured reflects the post-release inbox (S included). Assert the sentinel line is
  appended **after** S's line in the inbox file (byte order), not just present.
- Regression: drain with no in-flight send still appends the sentinel, returns the block,
  never restarts (existing pi tests keep passing).
- `mngr_pi_lifecycle_test.py`: unchanged — the extension's `[…, S, RETRACT]` handling
  (inject-then-discard across two ticks) is already covered; this plan only guarantees dwt
  produces that order.

## Open risks

- **Lock-path mismatch** (above): if dwt and mngr flock different files, the drain does
  not actually serialize and the race stays open silently. Verification item, not a
  runtime check — call it out explicitly in review.
- **Send that completed just before the lock was acquired.** The lock guarantees no send
  is *concurrently* appending, but a send whose flock released a beat before the drain
  acquired it has already put S in the inbox — which is fine (S is captured and ordered
  before the sentinel). There is no window where S's append is *pending* yet the lock is
  free: the append happens inside the lock. So acquiring the lock is a true barrier.
- **Kernel/FS visibility.** `get_queued_block` reads the inbox file after the appending
  process released the flock; POSIX ordering on a local fs makes S visible. Remote hosts
  are out of scope (the system interface runs local-only, per `tap.py:143`).
- **Double Stop / Stop then type.** A *new* message the user sends *after* Stop is a
  deliberate new turn, correctly not covered — the lock only wraps the in-flight send that
  was already going when Stop fired.

## Non-goals

- Any codex or claude change (codex has its own in-flight plan;
  `plan-codex-interrupt-inflight-lock.md`). claude's empty-queue chord path already locks;
  its nonempty-queue path uses SIGKILL, where an in-flight message is dropped-not-run —
  a separate, lesser gap not addressed here.
- The already-accepted capture-vs-consume race for a steer pi *committed* just before the
  tap (`plan-pi-interrupt.md` Open risks): that is a delivered message, not an in-flight
  one, and stays a visible-duplicate at worst.
