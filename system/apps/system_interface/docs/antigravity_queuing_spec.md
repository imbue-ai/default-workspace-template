# Antigravity (agy) queuing — a send-sourced outbox that feeds the shared queue

agy slots into the **existing** shoulder-tap queued-message system (the shared `QueuedSet`,
the `queued_messages` WS snapshot, `QueuedMessageView`, the `[Gently]` flush + composer
actions). It needs exactly one harness-specific piece — a queue **populator** — same as
claude, codex, and pi. The only twist is agy's enqueue *source* and its coalescing *leave*.

Everything downstream of the populator is untouched: no frontend changes, no new endpoint,
no new WS field. agy "just works" with the queue system once its populator feeds the set.

---

## 0. The shared system (recap — do not rebuild any of this)
- **`QueuedSet`** (`harnesses/queued_set.py`): FIFO of `QueuedMessage{queued_id, content,
  enqueue_ts, is_phantom}`. Mutators `add` / `resolve_oldest` / `resolve(id)` / `clear`;
  readers `snapshot()` (real entries → wire) and `concatenated_block()` (newline-joined turn).
- **Per-harness populator** maps that harness's raw signals onto those mutators. The ONLY
  harness-specific code. Examples: `claude/queue_tracker.py` (reconstructs enqueue/leave from
  the transcript ledger), `pi_coding/queue_tracker.py` (tails mngr's `pi_inbox` file).
- **Watcher owns the populator** and exposes the base hooks (`session_watcher.py`, default
  no-ops): `set_queue_snapshot_callback`, `get_queued_messages`, `get_queued_block`,
  `clear_queue`, `notify_idle`. `app_context.get_or_create_watcher` bridges the snapshot to
  `agent_manager.update_queued_messages` and registers `notify_idle` as the working→IDLE
  backstop.
- **Downstream (all shared)**: `queued_messages` on the agents WS state → `QueuedMessageView`
  renders the group + `[Gently]` button → `POST /flush-queue` / composer action, both using
  `concatenated_block()`.

Each populator is defined by two signals and one backstop:
- **enqueue** — a message the user parked (added to the FIFO tail),
- **leave** — a parked message committed (dropped from the FIFO head),
- **working→IDLE → clear** — the backstop for interrupts / crashes / flush-restart.

---

## 1. agy's problem: no enqueue ledger. The outbox IS the ledger.

pi reads `pi_inbox` (mngr appends every outgoing message there); claude reconstructs the
ledger from its transcript. **agy has neither** — verified: mngr_antigravity writes no inbox,
and a message queued while agy is busy does NOT appear in agy's transcript until it drains.
So agy has no on-disk source the watcher can tail for **enqueue**.

**The enqueue source is our own send.** When the UI sends a message, that IS the parked
message — agy accepted it (mngr confirms) but won't act until the current turn ends. So the
populator is a **send-sourced outbox**: the list of messages we sent that have not yet drained
into the transcript. This is the "custom outbox" — the agy analogue of `pi_inbox`, except the
ledger is the UI's own send record rather than a file. **leave** and the **backstop** are the
same as every harness (drained `user_message` / working→IDLE).

---

## 2. Design — `AntigravityQueueTracker` (mirrors `PiQueueTracker`)

`harnesses/antigravity/queue_tracker.py`, wrapping one shared `QueuedSet`:

| method | driver | action |
|---|---|---|
| `enqueue(content, timestamp)` | **the send path** (§3) | `QueuedSet.add(mint_id(...), content, timestamp, is_phantom=blank)` — append to FIFO tail |
| `leave(drained_content)` | **the watcher** (each newly-ingested `user_message`) | drop FIFO head(s) — **coalescing-aware (§4)** |
| `on_idle()` / `clear()` | working→IDLE backstop / flush restart | `QueuedSet.clear()` |
| `reset()` | re-attach / truncation | fresh `QueuedSet` |
| `snapshot()` / `concatenated_block()` | shared surface | delegate to the set |

`mint_id`: stable synthetic id salted by a monotonic enqueue counter + content (so two
identical sends get distinct ids; stable for the rendered bubble). Not a correlation key —
resolution is positional, exactly like pi/claude.

The FIFO conservation holds uniformly: **every UI send enqueues one; every drained
`user_message` leaves (pops).** A message sent to an idle agent enqueues then drains within
~1s and pops right back off — so, as pi already does, the snapshot push is **debounced** by a
short stability window so that transient enqueue never flickers as "queued".

---

## 3. Wiring the enqueue (the one new seam)

The watcher owns the populator (like pi), but agy's enqueue comes from the send, which the
watcher can't observe. Bridge it with one base-watcher hook:

- **`session_watcher.py`**: add `note_sent_message(self, content: str, timestamp: str) -> None`
  — a concrete **no-op default** (so no other harness changes), alongside the existing queue
  hooks.
- **`AntigravitySessionWatcher.note_sent_message`** → `self._queue_tracker.enqueue(...)` then
  push the (debounced) snapshot via the registered callback.
- **The send endpoint** (`server` `POST /api/agents/:id/message`) already resolves both the
  agent and the watcher; on a successful `send_message_to_agent`, call
  `watcher.note_sent_message(content, now_iso)`. (No-op for non-agy harnesses, whose watchers
  keep the default.)

leave / idle / snapshot need **no** new wiring — they use the existing base hooks the watcher
already overrides: the antigravity watcher, on each newly-ingested `user_message` event
(its existing scan), calls `self._queue_tracker.leave(event_content)`; `notify_idle` and
`set_queue_snapshot_callback` are the standard overrides `app_context` already invokes.

---

## 4. Coalescing — the flag biases `leave`

agy joins ALL pending queued messages with newlines into ONE `user_message` turn at turn-end
(verified: 3 queued → `"A\nB\nC"`). So one drained turn represents **N** parked messages
leaving. `leave` must pop N heads, not one. Drive that off the **queue-behavior flag**, so the
coalescing lives in one declared place, not hardcoded:

- **`QueueBehavior` enum** (`harnesses/model.py`, beside `SwitchMode`/`PickerMode`):
  `NORMAL = "normal"` (one drain pops one head — claude/codex/pi) · `COALESCES = "coalesces"`
  (one drain pops one head **per newline segment**). Add `queue_behavior: QueueBehavior =
  QueueBehavior.NORMAL` to `HarnessCatalog`; set `COALESCES` on `ANTIGRAVITY_CATALOG`. Default
  NORMAL ⇒ inert for every existing harness. The bias is applied **backend-side in the
  populator's `leave`**, not the frontend — the frontend stays dumb and just renders
  `queued_messages`.
- **`AntigravityQueueTracker.leave(drained_content)`**: with `COALESCES`, `resolve_oldest()`
  once per newline segment of `drained_content` (positional, FIFO — the combined turn is the
  join of the oldest N parked messages, so popping N heads is exactly right). With `NORMAL`
  it's a single `resolve_oldest()` (but agy only ever uses COALESCES).

  The tracker reads its behavior from the catalog at build (or is handed it), so the enum is
  the single source of truth for the bias.

Result: three queued bubbles (from three sends) render via the shared `queued_messages`
snapshot; when agy drains them as one combined turn the populator pops all three heads and the
snapshot empties — the shared `QueuedMessageView` clears the group. No frontend involvement.

---

## 5. Registration
Mirror pi/claude: the antigravity watcher builds its `AntigravityQueueTracker` in `build`,
overrides the queue hooks, drives `enqueue` (from `note_sent_message`) / `leave` (from drained
`user_message`s) / `clear` (`notify_idle`), and pushes debounced snapshots. No registry field
is needed (the populator lives in the watcher, as pi's does); the only cross-harness edits are
the `note_sent_message` base-hook default and the send-endpoint call (§3), plus the
`QueueBehavior` flag (§4).

---

## 6. The two shared actions — confirm they behave for agy
Both come free from the shared surface (`concatenated_block()`), but agy's interrupt=restart
makes them meaningful:
- **`[Gently]` shoulder-tap (`/flush-queue`)**: restart agy (clears its in-memory queue) and
  resend `concatenated_block()` (all parked messages, newline-joined) as one turn. Agy would
  have coalesced them anyway, so resending the block is the same shape — and now none are lost
  on the restart (the whole point). `clear_queue` empties the set after the restart.
- **Composer action** (hand the block back to the input): unchanged, harness-agnostic.

No agy-specific action code — the shared `_drain_queue`/flush already resend the whole block.

---

## 7. Build order
1. `QueueBehavior` enum + `HarnessCatalog.queue_behavior` (default NORMAL) + set COALESCES on
   `ANTIGRAVITY_CATALOG`. (~15 lines, inert for others.)
2. `antigravity/queue_tracker.py` (`AntigravityQueueTracker`, coalescing `leave`) + unit tests
   (enqueue/leave/coalesced-pop-N/idle-clear; mirror `pi_coding/queue_tracker_test.py`).
3. `session_watcher.py`: `note_sent_message` no-op default.
4. `AntigravitySessionWatcher`: own the tracker, override the queue hooks, drive leave from
   drained `user_message`s + push debounced snapshots + `note_sent_message` enqueue.
5. Send endpoint: call `watcher.note_sent_message` on a successful send.
6. Live verify: queue 3 to a busy agy agent → three bubbles via the shared group; on the
   combined drain they all clear; `[Gently]` restarts + resends the block.

## 8. Limitations (explicit)
- **UI-sent only** — a message typed directly into agy's terminal never enters our outbox (no
  send passed through us), so it has no pre-drain bubble. It still renders on drain as the
  normal turn. (Same as pi would have if a message bypassed `pi_inbox`.)
- **Positional resolution** — the outbox has no correlation id back to the drained turn, so
  leaves are FIFO/positional (like pi/claude); the working→IDLE backstop sweeps anything that
  never drains (interrupt/crash).
- **Session-dependent** — the queue is agy's TUI memory + our in-memory outbox; a restart /
  interrupt / backend restart clears it. Accepted (matches pi/claude).
