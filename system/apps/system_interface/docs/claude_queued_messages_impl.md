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
- **A message leaves the queue via exactly one leave-op: `dequeue`, `remove`, or
  `popAll`** — this is the conservation law (`enqueue = dequeue + remove +
  popAll`, verified exactly). Resolution keys off THESE OPS, positionally
  (drop the FIFO head per leave-op). Do NOT try to key resolution off the
  `queued_command` attachment or the committed `user` record's `promptSource` —
  that was tried and is WRONG (see the box below).
- **Task-notifications ride the same queue** and must NOT surface. They appear as
  `enqueue` records whose content begins `<task-notification>`, and they consume a
  leave-op like any message. So they are tracked as **phantom** placeholders: they
  occupy a FIFO slot (keeping positional resolution aligned) but are filtered out
  of the snapshot. A blank enqueue is a phantom too.

> **Why not `promptSource`/`queued_command` (a bug the preview caught).** An
> earlier model resolved only on a `queued_command` (`commandMode=="prompt"`) or a
> `user` record with `promptSource=="queued"`, ignoring the `dequeue`/`remove`
> ops. It passed 865 tests and then **stranded a committed message in the queued
> group on a real conversation.** Reason: in the real Minds flow every message is
> delivered via `mngr` (typed into the TUI), so a mid-turn message commits via a
> `dequeue` whose `promptSource` is `"typed"`, NOT `"queued"`; slash commands
> (`/fast`) and task-notifications behave likewise. Those resolutions were
> invisible, entries piled up, and later resolves dropped the wrong (oldest) one.
> Keying on the leave-ops with phantom placeholders nets exactly to the
> conservation law and reproduces empty on the real session. `promptSource` and
> `queued_command` are no longer used for resolution at all.

## 4. Backend: the tracker

One tracker per session, keyed on `sessionId`, reset on a new session file. Pure
function of the ledger; holds no UI state.

Entries carry a `kind` (`real` | `phantom`); only `real` entries are surfaced.
Resolution is one operation — drop the FIFO head — fired by every leave-op.

```python
@dataclass(frozen=True)
class QueuedMessage:
    queued_id: str      # sha1(f"{session_id}\0{enqueue_ts}\0{content}")[:16] — stable across replays
    content: str
    enqueue_ts: str
    is_phantom: bool    # task-notification / blank — occupies a FIFO slot, never surfaced

_TASK_NOTIFICATION_PREFIX = "<task-notification>"

class ClaudeQueueTracker:
    pending: list[QueuedMessage]   # FIFO, oldest first (real + phantom interleaved)

    def on_enqueue(self, ts, content):
        phantom = content.startswith(_TASK_NOTIFICATION_PREFIX) or not content.strip()
        self.pending.append(QueuedMessage(_qid(session_id, ts, content), content, ts, phantom))

    def on_leave(self):                        # dequeue OR remove OR popAll (one record)
        if self.pending:
            self.pending.pop(0)                # POSITIONAL — drop the oldest, phantom or real

    def on_idle(self):                         # backstop (§5)
        self.pending.clear()

    def snapshot(self):                        # what goes on the wire — REAL entries only
        return [{"queued_id": m.queued_id, "content": m.content, "timestamp": m.enqueue_ts}
                for m in self.pending if not m.is_phantom]
```

That is the whole model. `enqueue` adds (real or phantom); every `dequeue`,
`remove`, and `popAll` record drops the FIFO head; the snapshot hides phantoms.
This matches the conservation law exactly, so it is self-balancing: replaying the
whole ledger nets to precisely the still-pending real messages. Verified on the
real worker session (mngr-delivered message + `/fast` slash command + four
task-notifications + inline and dequeue commits) — nets to **empty**, where the
buggy model stranded one message.

Notes:
- `popAll` emits one record per flushed message; treating each as one `on_leave`
  (drop head) is uniform with dequeue/remove and needs no special "clear all".
- We do **not** read `promptSource` or the `queued_command` attachment for
  resolution at all (they broke — see §3 box). The `queued_command` attachment is
  still parsed *for the transcript* (it renders the committed inline turn), just
  not for queue tracking.
- `auto-continuation`: a rare non-human `commandMode=="prompt"` enqueue is
  indistinguishable at enqueue and is added as `real`; it resolves normally on its
  leave-op and may briefly appear. Rare; the idle backstop mops residue.

Wiring:
- Feed each raw session line to the tracker in the Claude watcher's
  forward-consume loop (NOT `parse_session_lines` — it drops `queue-operation`
  records at the `uuid` guard). `parse_queue_signals(line)` recognizes only the
  four `queue-operation` ops (`enqueue` → ADD with the phantom flag; `dequeue` /
  `remove` / `popAll` → LEAVE) and returns `None` for everything else.
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

| transition | detected by | action |
|---|---|---|
| becomes queued (real) | `queue-operation/enqueue`, content NOT `<task-notification>`/blank | append REAL |
| becomes queued (phantom) | `queue-operation/enqueue`, `<task-notification>`/blank | append PHANTOM (never surfaced) |
| leaves the queue | `queue-operation/dequeue` OR `remove` OR `popAll` (per record) | **positional** — drop FIFO head |
| stale survivor (SIGKILL / crash) | working→IDLE | clear all |

The only use of content is the `<task-notification>` prefix check (a structural
marker, not fuzzy matching) and salting the `queued_id` hash. Timestamps are never
a join key. `promptSource` and the `queued_command` attachment are NOT used for
resolution (they broke — §3 box); the attachment is still parsed for the
transcript render only.

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
  the user-bubble *view* (not `classifyUserMessage`).
- **Header row** above the topmost queued message: a label **"Queued messages"**
  on the LEFT and the **[Shoulder tap]** button on the RIGHT of the same row,
  styled as a subtle group header consistent with the chat UI.
- **[Shoulder tap]** button (in that header row) → `POST /flush-queue`; disabled
  while in flight. On hover it shows a tooltip: **"Gently interrupt model to send
  queued instructions"** (use the same CSS `data-tooltip` pattern the progress-view
  markers use; native `title` is unreliable in the webview).
- **The interrupt/drain action is the bottom composer Stop button, NOT a button on
  the queued group.** Restore the Stop button in the bottom chat bar (where it used
  to live). It calls `POST /drain-to-composer` (restart the agent and drop the
  returned combined block into the composer). Do NOT render a separate
  "[Interrupt → composer]" button above the queued group — the queued group has
  only the [Shoulder tap] button.
- No new `UserMessageKind`. Committed queued messages render as ordinary turns via
  the paths that already exist.

## 9. Tests (real fixtures already on host)

Unit-test `ClaudeQueueTracker.snapshot()` against recorded ledger sequences:

1. `enqueue` real → snapshot has 1 → `dequeue` → snapshot empty.
2. `enqueue` real → `remove` → snapshot empty (inline commit's leave-op).
3. `enqueue`×3 real → `popAll`×3 → snapshot empty.
4. Interleaved: `enqueue A`, `enqueue B`, one leave-op → head A dropped, snapshot
   has B; second leave-op → empty.
5. **Task-notification phantom:** `enqueue`(`<task-notification>…`) then
   `enqueue`(real human) → snapshot has ONLY the human; the task-notif's leave-op
   drops the phantom head, leaving the human; the human's leave-op empties it.
   Assert the human is never dropped by the phantom's resolution and the
   `<task-notification>` blob never surfaces.
6. **Interrupt:** `enqueue` real → `dequeue` (interrupt) → snapshot empty (a
   leave-op is a leave-op); independently, an unresolved real entry + working→IDLE
   → empty (idle sweep).
7. **REGRESSION (the preview bug):** the exact real worker session — a
   mngr-delivered message + a `/fast` slash command + four task-notifications, with
   dequeue and remove commits — must net to **empty**. This is the case the buggy
   `promptSource`/`queued_command` model stranded. Commit this session's ledger as
   a fixture and assert `snapshot() == []` after full replay.
8. Full-replay determinism: feed a whole real session's ledger from byte 0 →
   snapshot equals the live-tail result (self-correcting, no cursor).
9. Duplicate content: two `enqueue`s of identical text → two entries; one leave-op
   leaves exactly one.

Fixtures: the five recorded sessions under
`~/.claude/projects/-home-user-workspace/*.jsonl` PLUS the worker's own session
for the regression (test 7) — assert the conservation law
(`enqueue == dequeue + remove + popAll`) as the invariant, not raw counts (which
drift). The buggy model would fail test 7; the corrected model passes it.

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
