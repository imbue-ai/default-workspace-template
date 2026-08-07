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

Timing / correlation facts that drive the design (measured across 5 sessions,
two independent reviews):
- The queue is **FIFO**, and there is **no correlation id** on any record. So
  resolution is **positional** (drop the oldest still-pending message when a
  message leaves the queue).
- `enqueue.timestamp` and `queued_command.attachment.timestamp` are *usually*
  identical, but they are **independently clocked** — a real **1 ms skew** was
  found (`…928Z` enqueue vs `…927Z` attachment). So an exact-timestamp join is
  NOT safe: use it only as a sanity assertion, never as the join. Position is the
  join.
- `remove.timestamp` is the **remove moment**, NOT the enqueue moment. Combined
  with the above, we **ignore both the `remove` op and the raw `dequeue` op** for
  resolution (see below) and key off the two records that unambiguously mark a
  message leaving as a *human* turn.
- `popAll.timestamp` is the flush moment, shared across the batch. `popAll` =
  "clear all" (§4).
- **Task-notifications ride the same queue.** Background-task notices appear as
  `enqueue` records whose content begins `<task-notification>`, and they resolve
  through the same ops. They are NOT user turns and must never surface — skip them
  at enqueue by that content prefix (a structural-marker check, categorically
  different from the banned fuzzy content reconciliation).
- **`dequeue` is overloaded** and must not be treated as "committed": it also
  fires on user interrupt (the following `user` record is
  `[Request interrupted by user]`, `promptSource` absent) and on task-notification
  drains (`promptSource:"system"`). The clean discriminator is the resolving
  `user` record's `promptSource`: `"queued"` = a real human new-turn commit.

## 4. Backend: the tracker

One tracker per session, keyed on `sessionId`, reset on a new session file. Pure
function of the ledger; holds no UI state.

```python
@dataclass(frozen=True)
class QueuedMessage:
    queued_id: str      # sha1(f"{session_id}\0{enqueue_ts}\0{content}")[:16] — stable across replays
    content: str
    enqueue_ts: str     # verbatim ISO string from the enqueue record

_TASK_NOTIFICATION_PREFIX = "<task-notification>"

class ClaudeQueueTracker:
    pending: list[QueuedMessage]   # FIFO, oldest first

    def on_enqueue(self, ts, content):
        if content.startswith(_TASK_NOTIFICATION_PREFIX) or not content.strip():
            return                             # not a user turn — never surfaces
        self.pending.append(QueuedMessage(_qid(session_id, ts, content), content, ts))

    def on_inline_commit(self, attachment_ts=None):   # queued_command, commandMode == "prompt"
        # A message left the queue inline (the remove path). Drop the oldest.
        if self.pending:
            self.pending.pop(0)                # POSITIONAL. attachment_ts, if present,
                                               # may assert == head.enqueue_ts (skew-tolerant: log, don't rely)

    def on_new_turn_commit(self):              # user record with promptSource == "queued"
        if self.pending:
            self.pending.pop(0)                # POSITIONAL — the FIFO head opened its own turn

    def on_pop_all(self):                      # bulk flush
        self.pending.clear()

    def on_idle(self):                         # backstop (§5) — also covers interrupts
        self.pending.clear()

    def snapshot(self):                        # what goes on the wire
        return [{"queued_id": m.queued_id, "content": m.content, "timestamp": m.enqueue_ts}
                for m in self.pending]
```

We deliberately do **not** read the raw `dequeue` or `remove` ops — they are
overloaded/mis-timed (§3). Each message leaves the queue via exactly one surfaced
signal: `on_inline_commit` (a `commandMode=="prompt"` `queued_command`) OR
`on_new_turn_commit` (a `promptSource=="queued"` `user` record) OR `on_pop_all`.
The conservation law guarantees no double-resolve; a resolve on an empty list is a
harmless no-op. End-to-end simulation over all five real sessions nets to zero
pending except one genuinely-queued message in the still-live session.

Wiring:
- The Claude watcher tails the session JSONL. Feed each raw line to the tracker in
  the watcher's forward-consume loop (NOT `session_parser.parse_session_lines` —
  that drops `queue-operation` records at its `uuid` guard, since they are not DAG
  nodes). Add `parse_queue_signals(line)` that recognizes the four ops plus the
  `queued_command` attachment and the `promptSource:"queued"` user record; one
  record can feed both the transcript parse and the tracker.
- `commandMode != "prompt"` attachments (task-notifications) are ignored by
  `on_inline_commit` — they were never added at enqueue, so nothing to resolve.
- `auto-continuation` note: a rare `commandMode=="prompt"` `queued_command` with
  `origin.kind=="auto-continuation"` exists (1 in 50). It is indistinguishable
  from human at enqueue (enqueue carries no `origin`), so it is added and then
  resolved normally — it may briefly appear in the group and self-clears. Do not
  over-engineer; the idle backstop mops any residue.
- Reset the tracker (empty `pending`) on `SessionStart` / new session file.

Self-correcting on a backend restart: replaying the whole ledger nets every
resolved enqueue against its resolution, leaving exactly the still-pending set —
**no cursor / high-water mark needed.** The one gap (a SIGKILL drops messages
without writing a resolution) is closed by §5.

## 5. The one backstop

**Working → IDLE clears the whole pending list.**

Invariant: if Claude is IDLE, its queue is drained (a queued message would have
opened a turn, so it would not be idle). Therefore any `pending` survivor at a
genuine IDLE transition is stale → drop it. This single rule sweeps every gap we
deliberately don't track: **user interrupts** (a `dequeue` → `[Request
interrupted by user]`, which IS a real retract — the earlier claim that no
retract exists was wrong), our flush-restart SIGKILL, a crash, an OOM — none of
which produce a `prompt`/`queued` resolution signal, and all of which end at IDLE.
The activity layer already computes the working→IDLE transition (it drove the old
`clearQueuedMessagesOnIdle`); reuse that signal to call `tracker.on_idle()`.
Caveat: Claude has no real turn-boundary event; its "working" is a stale-tail
heuristic, so this backstop is soft — acceptable because it only ever *clears*,
never resurrects.

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

This is the whole point. Every correlation is **positional** (FIFO) or a
**structural-marker** check; nothing compares normalized text, and nothing relies
on a timestamp (which can skew).

| transition | detected by | resolve |
|---|---|---|
| becomes queued | `queue-operation/enqueue`, content NOT `<task-notification>` | append (none) |
| committed inline | `queued_command`, `commandMode=="prompt"` | **positional** — drop FIFO head |
| committed as new turn | `user` record, `promptSource=="queued"` | **positional** — drop FIFO head |
| bulk flushed | `queue-operation/popAll` | clear all (none) |
| stale survivor (interrupt / SIGKILL / crash) | working→IDLE | clear all (none) |
| ignored (overloaded / mis-timed) | raw `dequeue` op, `remove` op | — |

The only use of content is the `<task-notification>` prefix skip (a structural
marker, not fuzzy matching) and salting the `queued_id` hash. Timestamps are never
a join key — at most a logged sanity assertion.

The old `reconcilePendingMessages` / `normalizeContentForMatch` (frontend) is
deleted; the codex `_dedup_queued_turn` text match is out of scope here (handled
later by the codex parity move).

Duplicate content: two identical messages are two FIFO entries; each is dropped by
its own positional turn (oldest-first) — never confused, because we never ask "are
these two the same text."

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
- Read `queued_messages` (full snapshot) from the WS per-agent state; replace the
  whole group on each push.
- Render the queued group below the last committed turn, in enqueue order, reusing
  the user-bubble *view* (not `classifyUserMessage`), with two buttons above the
  group (both disabled while in flight): **[Shoulder tap]** → `POST /flush-queue`,
  and **[Interrupt → composer]** → `POST /drain-to-composer` then drop the
  returned block into the composer (see `shoulder_tap_spec.md` §8).
- No new `UserMessageKind`. Committed queued messages render as ordinary turns via
  the paths that already exist (`queued_command` attachment for inline;
  `promptSource:"queued"` `user` record for the new-turn commit).

## 9. Tests (real fixtures already on host)

Fixtures are extractable from `~/.claude/projects/-home-user-workspace/*.jsonl`.
Unit-test `ClaudeQueueTracker.snapshot()` against recorded ledger sequences:

1. `enqueue` → snapshot has 1 → `user`(`promptSource:"queued"`) → snapshot empty.
2. `enqueue` → `queued_command`(`commandMode:"prompt"`) → snapshot empty (inline).
3. `enqueue`×3 → `popAll` → snapshot empty.
4. Interleaved: `enqueue A`, `enqueue B`, `queued_command`(prompt) → head A
   dropped, snapshot has B; then `user`(queued) → empty.
5. **Task-notification skip:** `enqueue`(`<task-notification>…`) → snapshot empty
   (never added); a following `dequeue`/task-notif `queued_command` does not
   disturb a real human entry queued alongside it.
6. **Interrupt:** `enqueue` → `dequeue` → `user` with `[Request interrupted by
   user]` (no `promptSource:"queued"`) → snapshot still has the entry (we ignore
   the raw dequeue) → working→IDLE → empty (idle sweep).
7. **Timestamp skew:** `enqueue`(ts `…928Z`) → `queued_command`(ts `…927Z`) →
   snapshot empty — resolution is positional, so the 1 ms miss does not strand it.
8. Full-replay determinism: feed a whole real session's ledger from byte 0 →
   snapshot equals the live-tail result (self-correcting, no cursor).
9. Duplicate content: two `enqueue`s of identical text → two entries; one
   positional resolve leaves exactly one.

Use the recorded real sessions as fixtures — extract the ledger lines from
`~/.claude/projects/-home-user-workspace/*.jsonl` (five sessions; the conservation
law and `remove == queued_command` are the invariants to assert, not the raw
counts, which drift as the host accrues sessions).

Do NOT write tmux/interactive tests for the flush restart (flaky, per repo
guidance) — verify that manually.

## 10. Out of scope (deliberately)

- **codex.** Parity comes after Claude works: rewrite the fork to emit a
  `queued_committed` record carrying `queued_id`, giving codex the same ledger and
  deleting `_dedup_queued_turn`'s content match. Tracked in `shoulder_tap_spec.md`
  §6/§12.
- **Durable cursor.** Not needed for Claude (§4 self-corrects; §5 sweeps).
- **Optimistic "sending" state.** Rejected — dumb frontend, snapshot only.
- **codex populator.** After Claude; ideally after the fork emits a resolution
  record carrying `queued_id` so it drops `_dedup_queued_turn`'s content match.

(Both actions — shoulder-tap flush and interrupt-to-composer — ARE in scope; they
are common code on the shared entity, see `shoulder_tap_spec.md` §8.)

## 11. Placement + build order

New files (both harness-agnostic except the tracker):
- `harnesses/queued_set.py` — the common `QueuedSet` entity (`add`,
  `resolve_oldest`, `clear`, `snapshot`, `concatenated_block`). No harness code.
- `harnesses/claude/queue_tracker.py` — `ClaudeQueueTracker`, the pure Claude
  populator wrapping one `QueuedSet`. The ONLY harness-specific code.
- `parse_queue_signals()` added to `harnesses/claude/session_parser.py` (the four
  ops + the `queued_command` attachment + the `promptSource:"queued"` user record;
  `queue-operation` records are otherwise dropped at the DAG `uuid` guard).

Wiring points (existing files): the watcher's forward-consume loop feeds the
tracker; `_recompute_activity_state` fires `on_idle()` on working→IDLE;
`queued_messages` rides `get_agents_serialized()` beside `activity_state`.

Order (each step independently reviewable; 1–3 are pure additions):
1. `QueuedSet` entity + unit tests (hand-built entities). No harness, no UI.
2. `ClaudeQueueTracker` + `parse_queue_signals` + watcher wiring + `on_idle`.
   Unit tests against the recorded real sessions (§9).
3. `queued_messages` full snapshot on the WS state.
4. Common actions: `_drain_queue` helper + `POST /flush-queue` +
   `POST /drain-to-composer`.
5. Frontend tear-out (§8) + queued group + the two buttons. `handleSend` POST-only.
6. codex populator (later, live-validated).
