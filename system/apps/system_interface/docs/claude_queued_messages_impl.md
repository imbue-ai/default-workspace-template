# Claude queued messages — implementation spec (minimal)

The concrete build for Claude. Decisions are locked (background and the codex
delta live in `shoulder_tap_spec.md`; where the two disagree, this file wins for
Claude implementation).

Grounded in measured data: four real session JSONLs on this host, Claude Code
**2.1.207** (enqueue 39, remove 27, dequeue 8, popAll 4).

---

## 1. Principle

The frontend is dumb. State flows down, intents flow up.

- **Down:** the transcript events (committed turns) and a **full snapshot** of the
  currently-queued messages. The frontend renders both verbatim and holds no
  derived state of its own.
- **Up:** send and shoulder-tap are POSTs. They paint nothing locally; the next
  state push reflects what they did.

No optimism. No fuzzy text reconciliation anywhere (see §7).

## 2. The invariant

> A message is **queued** iff it has an `enqueue` with no matching resolution
> (`dequeue`, inline-commit, or `popAll`) yet.

The queued set is computed from Claude's own queue ledger. We never match a
queued message to a committed turn by text; we watch messages enter and leave the
queue.

## 3. Records consumed (real shapes)

**`queue-operation`** — out-of-band ledger lines in the session JSONL (not DAG
nodes; no uuid/promptId). Conservation law holds exactly:
`enqueue = dequeue + remove + popAll`.

```json
{"type":"queue-operation","operation":"enqueue","timestamp":"2026-08-07T06:33:06.983Z","sessionId":"…","content":"<text>"}
{"type":"queue-operation","operation":"dequeue","timestamp":"…","sessionId":"…"}
{"type":"queue-operation","operation":"popAll","timestamp":"<flush-moment>","sessionId":"…","content":"<text>"}
```

**`queued_command` attachment** — a DAG node written at commit time, but stamping
the **enqueue** timestamp. 1:1 with `remove` (per-session counts equal). Already
parsed today by `_parse_queued_command_attachment`.

```json
{"type":"attachment","uuid":"…","parentUuid":"…","timestamp":"2026-08-07T06:33:06.983Z",
 "attachment":{"type":"queued_command","prompt":"<text>","commandMode":"prompt","timestamp":"2026-08-07T06:33:06.983Z"}}
```

Timing facts that drive the design (measured):
- `enqueue.timestamp` **==** `queued_command.attachment.timestamp` (identical, both
  the enqueue moment). This is our exact join key.
- `remove.timestamp` is the **remove moment** (e.g. `06:33:34`), NOT the enqueue
  moment (`06:33:06`). So the `remove` op is useless for an exact join — **we
  ignore it** and use the `queued_command` attachment instead.
- `popAll.timestamp` is the flush moment, shared across the batch (not enqueue
  times). So `popAll` cannot be joined to enqueues either — we treat it as
  "clear all" (§4).
- `dequeue` carries no content and no enqueue reference → matched positionally
  (FIFO head).

## 4. Backend: the tracker

One tracker per session, keyed on `sessionId`, reset on a new session file. Pure
function of the ledger; holds no UI state.

```python
@dataclass(frozen=True)
class QueuedMessage:
    queued_id: str      # sha1(f"{session_id}\0{enqueue_ts}\0{content}")[:16] — stable across replays
    content: str
    enqueue_ts: str     # verbatim ISO string from the enqueue record

class ClaudeQueueTracker:
    pending: list[QueuedMessage]   # FIFO, oldest first

    def on_enqueue(self, ts, content):
        self.pending.append(QueuedMessage(_qid(session_id, ts, content), content, ts))

    def on_dequeue(self):                      # committed as a NEW turn
        if self.pending:
            self.pending.pop(0)                # FIFO head (positional)

    def on_queued_command(self, attachment_ts):  # committed INLINE (the remove path)
        for i, m in enumerate(self.pending):
            if m.enqueue_ts == attachment_ts:    # EXACT timestamp equality — no text compare
                self.pending.pop(i)
                return

    def on_pop_all(self):                      # bulk flush
        self.pending.clear()

    def on_idle(self):                         # backstop (§5)
        self.pending.clear()

    def snapshot(self):                        # what goes on the wire
        return [{"queued_id": m.queued_id, "content": m.content, "timestamp": m.enqueue_ts}
                for m in self.pending]
```

Wiring:
- The Claude watcher already tails the session JSONL and parses each record. Add
  the four hooks (`enqueue`/`dequeue`/`popAll` from `queue-operation`;
  `on_queued_command` from the existing `queued_command` attachment parse — one
  record, two consumers).
- `commandMode == "task-notification"` attachments are NOT user turns → do not
  call `on_queued_command` for them (existing parse already filters to `prompt`).
- Reset the tracker (new empty `pending`) on `SessionStart` / new session file.

Self-correcting on a backend restart: replaying the whole ledger nets every
resolved enqueue against its resolution, leaving exactly the still-pending set —
**no cursor / high-water mark needed.** The one gap (a SIGKILL drops messages
without writing a resolution) is closed by §5.

## 5. The one backstop

**Working → IDLE clears the whole pending list.**

Invariant: if Claude is IDLE, its queue is drained (a queued message would have
opened a turn, so it would not be idle). Therefore any `pending` survivor at a
genuine IDLE transition is stale → drop it. This single rule sweeps every ledger
gap: our flush-restart SIGKill, a crash, an OOM — none write a resolution record,
and all end at IDLE. The activity layer already computes the working→IDLE
transition (it drove the old `clearQueuedMessagesOnIdle`); reuse that signal to
call `tracker.on_idle()`.

## 6. Surface + the flush intent

**Snapshot field.** Add `queued_messages: QueuedMessage[]` to the per-agent state
already pushed over the WebSocket (`/api/ws`, alongside `activity_state`).
**Full snapshot every push** — the frontend replaces its queued group wholesale;
it never diffs or accumulates.

**Flush endpoint.** `POST /api/agents/:id/flush-queue`:
1. Read `tracker.pending` (backend holds it). If empty → 200 no-op.
2. `mngr start <agent> --restart --no-resume` (history preserved; in-harness queue
   dropped by SIGKILL; refused only for `is_primary=true`, which our chat agents
   are not).
3. Re-deliver as ONE combined message: `"\n".join(m.content for m in pending)`, in
   enqueue order, via the existing `send_message` path (waits for TUI ready after
   restart; STRICT submission confirmation).
4. `tracker.pending.clear()`. The IDLE transition after the restart also fires
   `on_idle()`, mopping up the ledger's now-orphaned enqueues on any subsequent
   full replay.

Combining is required: after restart the agent is idle, so sending one at a time
would let the first open a turn and the rest re-queue.

## 7. No fuzzy matching — the join table

This is the whole point. Every correlation is positional or exact-equality; none
compares normalized text.

| transition | how it's detected | join |
|---|---|---|
| becomes queued | `queue-operation/enqueue` | append (none) |
| committed as new turn | `queue-operation/dequeue` | **positional** — FIFO head |
| committed inline | `queued_command` attachment | **exact** — `enqueue_ts == attachment.timestamp` |
| bulk flushed | `queue-operation/popAll` | clear all (none) |
| stale survivor | working→IDLE | clear all (none) |

Content is used only to (a) display and (b) salt the `queued_id` hash. It is
never compared to decide "are these two the same message." The old
`reconcilePendingMessages` / `normalizeContentForMatch` (frontend) is deleted;
the codex `_dedup_queued_turn` text match is out of scope here (handled later by
the codex parity move).

Duplicate content: two identical messages get distinct `enqueue_ts` (distinct
`queued_id`), are removed by their own exact ts or by FIFO order — never confused.

## 8. Frontend changes

**Delete**
- `frontend/src/models/PendingMessages.ts`, `frontend/src/views/PendingMessageView.ts`.
- The forced-THINKING override (`getEffectiveActivityState`),
  `initQueuedMessageIdleClearing`, `clearQueuedMessagesOnIdle`, and every
  `reconcilePendingMessages` call site (`Response.ts`, `ChatPanel.ts`).

**Change**
- `MessageInput.handleSend()` → POST and return. No optimistic bubble, no status,
  no rollback.

**Add**
- Read `queued_messages` from the WS per-agent state.
- Render the queued group below the last committed turn, in enqueue order, reusing
  the user-bubble *view* (not `classifyUserMessage`), with one **[Shoulder tap]**
  button above the group → `POST /flush-queue`; disable while in flight; replace
  the whole group on each snapshot.
- No new `UserMessageKind`. Committed queued messages render as ordinary turns via
  the paths that already exist (`queued_command` attachment for inline;
  `promptSource:"queued"` `user` record for dequeue).

## 9. Tests (real fixtures already on host)

Fixtures are extractable from `~/.claude/projects/-home-user-workspace/*.jsonl`.
Unit-test `ClaudeQueueTracker.snapshot()` against recorded ledger sequences:

1. `enqueue` → snapshot has 1 → `dequeue` → snapshot empty.
2. `enqueue` → `queued_command`(same ts) → snapshot empty (inline commit).
3. `enqueue`×3 → `popAll` → snapshot empty.
4. Interleaved: `enqueue A`, `enqueue B`, `queued_command`(B's ts) → snapshot has
   A only; then `dequeue` → empty (head = A).
5. Full-replay determinism: feed a whole real session's ledger from byte 0 →
   snapshot equals the live-tail result (self-correcting, no cursor).
6. Idle sweep: `enqueue` with no resolution, then a working→IDLE → `on_idle()` →
   snapshot empty.
7. Duplicate content: two `enqueue`s of identical text → two distinct
   `queued_id`s; resolving one leaves the other.

Do NOT write tmux/interactive tests for the flush restart (flaky, per repo
guidance) — verify that manually.

## 10. Out of scope (deliberately)

- **codex.** Parity comes after Claude works: rewrite the fork to emit a
  `queued_committed` record carrying `queued_id`, giving codex the same ledger and
  deleting `_dedup_queued_turn`'s content match. Tracked in `shoulder_tap_spec.md`
  §6/§12.
- **Durable cursor.** Not needed for Claude (§4 self-corrects; §5 sweeps).
- **Flush-to-composer.** Rejected — shoulder tap restarts and resends inline.
- **Optimistic "sending" state.** Rejected — dumb frontend, snapshot only.

## 11. Build order

1. `ClaudeQueueTracker` + the four hooks in the watcher + reset on new session.
   Unit tests (§9). No UI change.
2. `queued_messages` full snapshot on the WS per-agent state; frontend reads and
   renders the queued group (still keep old optimism briefly to compare, then).
3. Delete the optimistic layer (§8); `handleSend` POST-only.
4. `POST /flush-queue` + the button.

Each step is independently reviewable; 1–2 are pure additions.
