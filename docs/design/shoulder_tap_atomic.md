# `shoulder_tap_atomic` — race-free interrupt-and-flush for codex & pi

Status: DESIGN (no code yet). Reviewers: attack this.

## 1. What we are building

A single harness-neutral operation our code can invoke:

```
shoulder_tap_atomic(agent, observed_turn_id) -> Outcome
```

Meaning: *if the turn I observed running is still the live turn, interrupt it so the
harness flushes its already-queued steer messages into one fresh merged turn, right now;
otherwise do nothing (the steers ride the next turn on their own).* It sends **no message
payload** — the user's queued messages are already in the harness's native steer queue.

This replaces today's shoulder-tap, which SIGKILLs and relaunches the agent
(`_flush_queue_endpoint` -> `_restart_agent_process` in
`system/apps/system_interface/imbue/system_interface/server.py`). The restart stays only
for harnesses that cannot interrupt natively (claude).

## 2. The race we must defeat (framed as a hazard)

Two asynchronous "cores":
- **Core U** = the system-interface server/UI (the "Shoulder tap" button).
- **Core H** = the harness process (pi or codex) actually running the turn.

They share no coherent memory. The naive design is a non-atomic cross-core read-modify-write:

```
if Core_H.is_running():   # stale LOAD
    Core_H.interrupt()    # much-later STORE
```

Between the LOAD and the STORE the turn can finish. Worse, for codex the "interrupt"
opcode is **state-aliased**: ESC decodes as "interrupt" while running but "open backtrack UI"
while idle. So a stale poke can execute the *wrong* instruction. **Raw tmux ESC is therefore
rejected on two independent grounds: no atomicity, and opcode aliasing.**

## 3. The mechanism: an ABA-safe compare-and-swap executed on the harness

We want one indivisible instruction:

```
CAS(live_turn, expected = observed_turn_id, then = interrupt_and_flush)
```

Two invariants make it correct:

### Invariant A — the CAS runs on Core H, on its single serial commit stage
Only the harness can compare its own live turn state and act on it with nothing interleaving.
Core U merely posts an *intent*; Core H executes the CAS.

- **pi**: the mngr lifecycle extension runs inside pi's **single-threaded JS event loop**. A
  callback runs to completion; nothing (including the turn's own `agent_end`) can run between
  two synchronous statements in it. The compare and the abort must therefore be in the **same
  synchronous tick with no `await` between them**.
- **codex**: core consumes an **ordered op-queue**, one op at a time. Our interrupt arrives as
  an `Op::Interrupt` (via a patched control channel, never a keystroke). "Turn N completed" is
  *also* an event on that same serial timeline, so the interrupt and the completion are
  strictly ordered — never overlapping. Either order yields a defined, non-torn result.

### Invariant B — the turn id is the ABA version tag
A bare "is it running?" boolean suffers ABA: turn N running (true) -> N ends -> N+1 starts
(true again). Same boolean, different turn. Interrupting on the boolean alone would abort
N+1. So the CAS compares the **generation-stamped** turn id, not a boolean.

- **codex**: emits `TURN_STARTED / TURN_COMPLETED / TURN_ABORTED` with ids (see
  `HarnessSpec.special_kinds` in `harnesses/registry.py`). The version register exists **for
  observation**. OPEN RISK (see §7): codex's `Op::Interrupt` may interrupt the *current* turn
  unconditionally and NOT gate on a target id — in which case the patch must add the
  id-guarded interrupt. Having ids in the transcript does not by itself make the interrupt
  ABA-safe.
- **pi**: has only a binary `active` marker — NO turn id. We must **add a monotonic turn
  counter to the extension** (bump on `agent_start`, expose it on the ctx / a state file), so
  pi's CAS can compare a generation, not a boolean.

## 4. Per-harness walkthrough

### 4.1 pi
1. Core U observes `active` + `turn_counter == N` (N is the new counter we add).
2. User clicks; Core U writes an intent line `{op: interrupt, target: N}` to a control file
   (mirrors the existing `pi_control.jsonl` model-switch drain in
   `mngr_pi_lifecycle.ts`).
3. Extension poll callback (one synchronous JS tick) does:
   ```
   const t = latestCtx;
   if (t && t.currentTurn === N && !t.isIdle()) t.abort();   // ABA-safe, atomic
   ```
   `ctx.isIdle()` is a synchronous boolean (`agent-session.ts:883`) and the check+call is one
   atomic tick — that part holds.
   **CORRECTION (see §9): `ctx.abort()` does NOT cleanly abort-and-leave-the-queue.** In
   interactive mode (how mngr runs pi) `ctx.abort` routes to `_extensionAbortHandler`, which is
   `restoreQueuedMessagesToEditor({abort:true})` (`interactive-mode.ts:1817-1824,4209-4228`):
   it `clearAllQueues()`, writes the steers into the TUI editor buffer **unsent**, then aborts.
   The `void this.abort()` fallback at `2425` is dead in TUI mode. So abort *removes* the
   steers from the native queue into an editor the extension cannot read (§9.1).
4. OPEN RISK (see §7): pi's *native* Esc handler does
   `restoreQueuedMessagesToEditor({abort:true})` (`interactive-mode.ts`) — abort **drains
   steers back to the composer**, it does NOT auto-flush them as a merged turn. So after
   `ctx.abort()` the extension must **explicitly resubmit the queued steers as one merged new
   turn** (`sendUserMessage`), or they will sit in the editor unsent. This is the biggest
   asymmetry with codex.
5. Extension writes an outcome marker `{turn: N, outcome: "aborted" | "already-idle"}`.

### 4.2 codex
1. Core U observes `TURN_STARTED(id = N)`.
2. User clicks; Core U writes intent `{op: interrupt, target: N}` to a patched control file.
3. Codex must, guarded on `current_turn_id == N && turn_active`, run the Esc-flush path:
   merge `pending_steers` into one fresh turn and append `queued_committed(queued_id)`.
   Mismatch/idle -> `queued_retracted` / no-op.
   **CORRECTION (see §9): this is NOT reachable today.** The 0.146 patch touches only TUI
   crates; the flush-vs-retract decision lives in `chatwidget/input_restore.rs`
   `on_interrupted_turn`, gated by the TUI flag `submit_pending_steers_after_interrupt`,
   which is set only by the Esc keypress. There is no `Op::Interrupt` control channel and no
   turn-id guard on the interrupt path. This is net-new patch work — see §9.2.
4. Outcome is read from `queued_input.jsonl` keyed by `queued_id`.

## 5. The steers are already queued (this is a flush, not a store)

A message typed to a running agent is already in the harness's native queue: pi's
`pending_steers` (delivered via `pi_inbox` -> `sendUserMessage(..., {deliverAs:"steer"})`,
already shipped) and codex's `pending_steers`. `shoulder_tap_atomic` carries no payload; it
only triggers the conditional flush. Input is `(agent, expected_turn_id)`.

DELIVERY-PIPELINE NOTE: there is a ~200ms path from "user typed" -> mngr inbox -> extension
poll -> `pending_steers`. If the button is hit inside that window, the message may not be in
`pending_steers` yet; the interrupt then aborts a turn with nothing to flush, and the message
lands as the next turn's steer regardless. No message lost; just not part of *this* flush.

## 6. Surfacing it: `HarnessSpec` flag + claude fallback

Add `native_atomic_shoulder_tap_possible: bool` to `HarnessSpec`
(`harnesses/registry.py`), mirroring the `switch_mode` precedent
(`harnesses/model.py` `SwitchMode`, surfaced to the frontend via the catalog and read in
`ModelBar.ts`).

- `CLAUDE = False` -> button keeps today's SIGKILL-restart flush.
- `CODEX = True`, `PI_CODING = True` -> button calls `shoulder_tap_atomic`.

Surface the flag to the frontend the same way `switch_mode` reaches `HarnessCatalog.ts`, and
branch the "Shoulder tap" button on it.

## 7. Known risks / open questions (REVIEWERS: attack these first)

1. **pi atomicity depends on no `await`.** If the interrupt drain callback is `async` and has
   an `await` between the `currentTurn`/`isIdle` check and `abort()`, atomicity is lost (a
   microtask — including `agent_end` — can interleave). The impl MUST keep check+abort
   synchronous. Is that actually achievable in the extension's timer callback?
2. **pi abort != flush.** Native Esc drains steers to the composer, not a merged turn (§4.1
   step 4). Does explicitly resubmitting after abort actually produce one merged turn, and
   does anything drop the steers between abort and resubmit?
3. **codex `Op::Interrupt` may not gate on turn id.** If it unconditionally interrupts the
   current turn, the ABA guard must be added in the patch. Does the 0.146 patch already carry
   a turn id into the interrupt path, or is this net-new patch work?
4. **codex observation vs action id skew.** Core U observes `TURN_STARTED(N)` from the
   transcript tail (stale). Is the id core compares against at op-time the same id space?
5. **Delivery-pipeline race (§5).** Is "abort a turn with empty `pending_steers`" ever harmful
   (e.g., wasted expensive turn, or a user-visible abort with no apparent effect)?
6. **`ctx.abort()` synchrony.** `abort: () => { void this.abort(); }` — the AbortController is
   set synchronously, but is there any path where the turn keeps producing (tool already
   in-flight) such that `isIdle()` lies or the flush races the tail?
7. **Outcome-marker correctness.** Can the pi marker or codex ledger record a
   committed/aborted outcome that disagrees with what actually happened (e.g., abort fired but
   the turn had already retired)?
8. **Multi-tab / concurrent shoulder-taps.** Two Core-U clients tap the same agent; both post
   intents. Does the serial commit stage make the second a clean no-op, or can they compound?

## 8. Non-goals
- Claude atomic interrupt (no surface; stays on restart).
- Streaming/partial rendering (separate track).
- Preempting a mid-flight tool call (interrupt acts at the model/turn boundary, not by killing
  a running bash command).

---

## 9. Adversarial review results (two independent reviewers converged)

Both a concurrency reviewer and a harness-behavior reviewer, working separately against the
real source, reached the same verdict: **the atomicity + ABA theory is sound; every
action-side mechanism as originally written is wrong or unbuilt.** What held: pi single-tick
atomicity (isIdle+call in one tick), the `pi_control.jsonl` intent-file precedent, codex turn
ids being observable, the per-steer `queued_id` ledger, and the claude restart fallback.

### 9.1 pi — `ctx.abort()` is the drain-to-editor, not a clean abort (CRITICAL)
`ctx.abort` → `_extensionAbortHandler` → `restoreQueuedMessagesToEditor({abort:true})`
(`agent-session.ts:2420-2426`, `interactive-mode.ts:1817-1824,4209-4228`). It empties the
native steer queue into the TUI editor buffer (unsent) and aborts. The extension's pi API
(`sendUserMessage/setModel/setThinkingLevel`, `mngr_pi_lifecycle.ts:147-152`) cannot read that
editor buffer, and the extension retained no copy of the steer text (it fire-and-forgets from
`pi_inbox`). So "abort then resubmit the queued steers" has nothing to resubmit, and the
"no payload" premise cannot hold for pi. Also: `agent.ts` re-queues via `prompt()` while
`isStreaming` is still true, so a naive resubmit right after abort just re-parks the steers,
stranded until the next user prompt. Net today: pi's atomic path would be a **regression** over
the working restart-flush (which actually recaptures and resends the block,
`server.py:749-802`), silently dropping the queued messages into the editor.

pi rework required (bigger than "a bool + a button branch"):
- Add a monotonic **turn counter**, and **persist/seed it across restart** (a fresh process
  resets it; naive reset re-aliases id N onto a different turn — a real ABA hole, §9.4).
- On interrupt intent, the extension must own the payload: re-read the specific `pi_inbox`
  lines for the parked steers and, once the abort settles to idle, resubmit them as one turn —
  without double-submitting the copy `restoreQueuedMessagesToEditor` dropped in the editor.
- Add an interrupt intent-file poller + an outcome-marker writer.
- Honest reassessment: because we already ship greedy `deliverAs:"steer"` (injected between
  tool rounds), pi's interrupt only adds value for a *long single generation/tool*. Given the
  rework cost and the drain-to-editor trap, greedy steering may already be pi's 90% solution;
  a true atomic interrupt is real work and lower-value than for codex.

### 9.2 codex — the flush path is not reachable by any op; net-new patch work (CRITICAL)
The 0.146 patch touches **only TUI crates** (`grep Op::Interrupt` = 0 matches outside a test).
The flush-vs-retract branch is `on_interrupted_turn` in `chatwidget/input_restore.rs`, decided
by the TUI flag `submit_pending_steers_after_interrupt` (patch test 2559-2583:
`false -> queued_retracted`, `true -> queued_committed`). That flag is set by the **Esc
keypress**; any externally-injected interrupt reaching `on_interrupted_turn` with the default
flag takes the **RETRACT** branch (steers dumped to composer, `queued_retracted`) — the
opposite of what we want. There is no turn-id guard on the interrupt path (the id
reconciliation exists only on the *steer* path: `active_turn_id_for_thread` + retry loop,
patch 1188-1202). `0.146.0-patch-details.md:101-107` explicitly declined a TUI→core path
("a new protocol request, a processor and a core handler across four crates").

### 9.3 HarnessSpec flag is on the wrong object (MAJOR)
`/api/harnesses` serializes `HarnessCatalog` (`server.py:465`, `model.py:163-181`), where
`switch_mode` actually lives. `HarnessSpec` (`registry.py:44-64`) holds watcher/tracker/resolver
CLASSES with `arbitrary_types_allowed` and is never serialized. So
`native_atomic_shoulder_tap_possible` must go on **`HarnessCatalog`** (or another serialized
payload), not `HarnessSpec`, to reach the frontend button.

### 9.4 New ABA hole: pi turn counter reset on restart (MAJOR)
A per-process counter reset by `pi --session` resume re-climbs and re-aliases a prior id N onto
a different turn; a stale `{target:N}` intent then interrupts the wrong turn. Invariant B needs
a *globally* monotonic generation — persist the counter (the extension already seeds other
counters from `countLines()` for exactly this reason, `mngr_pi_lifecycle.ts:322-329`).
`/new` / `session_switch` raise the same question.

### 9.5 Outcome marker can lie (MAJOR, matches §7.7)
codex writes `queued_committed` per steer *before* the merged resubmission is accepted
(patch 1763-1766) and the merged turn "carries no ledger id of its own" (patch 1762). Our
current reader also collapses committed vs retracted into one "leave"
(`session_parser.py:269`, `queue_tracker.py:44-48`) — distinguishing them needs new parsing.

---

## 10. Codex source changes required (answering "what needs rebuilding")

The `build.sh` rebuild itself is cheap. The **patch** is the work, and per §9.2 it is net-new.
Cheapest viable route keeps everything in the **TUI crate the patch already edits** (avoids the
4-crate core path `patch-details` warned about):

1. **Inbound trigger (TUI-side).** Add a small watcher/reader in the TUI layer for an interrupt
   intent (e.g. a control file `$CODEX_HOME/interrupt_request.jsonl` carrying `{target_turn_id}`),
   drained on the TUI's own event loop.
2. **Route to flush, not retract.** When it fires, set `submit_pending_steers_after_interrupt`
   (or the equivalent) to TRUE and invoke the same interrupt entry the Esc keypress uses, so
   `on_interrupted_turn` takes the merge/`queued_committed` branch.
3. **Turn-id guard (ABA).** Before triggering, compare the intent's `target_turn_id` to the
   active turn id (reuse the steer path's `active_turn_id_for_thread` reconciliation, patch
   1188-1202). Mismatch/idle -> do nothing.
4. **(Our side, no rebuild) distinguishing reader.** Extend `session_parser.py` /
   `queue_tracker.py` to tell `queued_committed` from `queued_retracted` so the outcome is
   truthful; optionally address the "commit stamped before resubmit accepted" ordering.

Then `./build.sh --version 0.146.0` (and a matching `patches/0.146.0-patch-details.md` +
`minds_*` test per the repo's contract). Sibling patch files exist per codex version.

---

## 11. Multi-queued messages

- **codex: YES, natively — and this is the whole point of the flush.** The interrupt path
  merges **all** of `pending_steers` into **one** fresh turn
  (`merge_user_messages_with_history_record(pending_steers)`, patch ~1768) and writes a
  `queued_committed` per original steer id. N queued messages -> one merged turn, each still
  individually identifiable in the ledger. (Once §10 makes the flush reachable.)
- **pi: does NOT merge — it injects steers individually/greedily.** `agent-loop.ts:167,259`
  re-polls `getSteeringMessages()` at start and after every tool round and injects each as its
  own user message (`182-190`). So with multiple queued messages pi delivers them one-by-one
  as it reaches tool boundaries; there is no "one merged turn." And on the interrupt path the
  §9.1 drain-to-editor trap applies to the whole batch. So "works for multi-queued" is
  codex-yes-merged, pi-sequential-and-currently-broken-until-the-§9.1-rework.
