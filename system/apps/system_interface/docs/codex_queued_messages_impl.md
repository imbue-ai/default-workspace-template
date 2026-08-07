# Codex queued messages — implementation spec (rewrite to match Claude)

Wire codex onto the SAME harness-agnostic queued-message machinery Claude already
uses, now that the `codex-in-minds` fork emits a full queue ledger. Tear out the
old codex approach (a placeholder bubble injected into the transcript, reconciled
by content-matching) and replace it with a `CodexQueueTracker` that populates the
shared `QueuedSet` — resolving **by id**, which is cleaner than Claude's
positional resolve.

Companion to `claude_queued_messages_impl.md` (the Claude build, shipped) and
`shoulder_tap_spec.md` §4.5 (the cross-harness contract). The codex signal spec
that the fork implemented is `codex_queuing_signals_spec.md`.

## 1. What the fork now emits (confirmed from the patch)

A full ledger at `$CODEX_HOME/queued_input.jsonl`, one JSON object per line:

```json
{"type":"queued_input","queued_id":"…","thread_id":"…","timestamp":"…","content":"…"}
{"type":"queued_committed","queued_id":"…","timestamp":"…"}
{"type":"queued_retracted","queued_id":"…","timestamp":"…"}
```

- `queued_input` at enqueue (the `pending_steers.push_back` site); carries content.
- `queued_committed` when a parked steer is injected/submitted to core.
- `queued_retracted` on every non-commit exit: `on_interrupted_turn`, the
  `rejected_steers_queue` path, and `restore_thread_input_state` (which closes
  **every live entry** as retracted on `codex resume` / thread switch).
- **Conservation:** each `queued_input` gets exactly one terminating record. So a
  full replay of the ledger nets to exactly the currently-pending set — the same
  self-correcting property as Claude, no durable cursor needed.
- Terminating records carry **no content** (id only) — content lives on the
  enqueue record.
- Bonus: the committed rollout `user_message` also carries `client_id ==
  queued_id`. We do **not** need it (the sidecar's `queued_committed` already
  resolves); ignore it for now.

## 2. The point (unchanged from Claude)

The queue-snapshot machinery is already harness-agnostic and carries Claude:

- `AgentSessionWatcher` (`harnesses/session_watcher.py`) has no-op queue methods
  (`get_queued_messages`, `get_queued_block`, `clear_queue`, `notify_idle`,
  `set_queue_snapshot_callback`).
- `agent_manager` caches + broadcasts the `queued_messages` snapshot
  (`update_queued_messages`) and runs the working→IDLE backstop (`notify_idle`).
- The WS `queued_messages` field, the `/flush-queue` + `/drain-to-composer`
  endpoints, and the entire frontend (`QueuedMessageView`, `OutgoingMessages`,
  `ChatPanel`) are common.

So wiring codex is: one populator (`CodexQueueTracker`) + one ledger parser +
watcher wiring. Everything downstream is untouched. The litmus test from
`shoulder_tap_spec.md` §4.5 holds — no `if harness == …` on any read/render path.

## 3. Tear out (the old codex approach)

`harnesses/codex/watcher.py`:
- `_consume_queued_input` — DELETE. It emitted a **placeholder `user_message`
  event into the transcript** for each `queued_input` line; we no longer put
  queued messages in the transcript at all (they ride the WS snapshot).
- `_dedup_queued_turn` + its call in the consume loop — DELETE. It **content-
  matched** the drained rollout turn to the placeholder to supersede it; with no
  placeholder in the transcript, the drained rollout `user_message` simply renders
  as an ordinary committed turn (already parsed by `parse_lines`' `event_msg` /
  `item_completed` UserMessage path). Nothing to dedup.
- `_queued_id_by_content` — DELETE (content-match map).
- Keep tailing `queued_input.jsonl` (the byte-cursor/partial-line machinery), but
  feed each line to the tracker (below) instead of emitting transcript events.

`harnesses/codex/session_parser.py`:
- `queued_input_event` — DELETE (built the placeholder event).
- `normalize_user_content` — DELETE (content-match helper).
- `QUEUED_INPUT_RECORD_TYPE` — subsumed by the new parser.

Result: codex stops injecting queued placeholders into the transcript and stops
content-matching. This is exactly the placeholder-in-transcript + fuzzy-reconcile
pattern that was already torn out on the Claude side.

## 4. Build (mirror Claude)

**4.1 Shared `QueuedSet` — add resolve-by-id.** `harnesses/queued_set.py` today
has `add`/`resolve_oldest`/`clear`/`snapshot`/`concatenated_block`. Add:

```python
def resolve(self, queued_id: str) -> None:
    """Remove the entry with this id (no-op if absent). Used by harnesses whose
    ledger names which message left (codex); Claude uses resolve_oldest."""
```

Harness-agnostic; Claude keeps `resolve_oldest`, codex uses `resolve`. codex has
no phantom concept (`pending_steers` are all real user steers), so it always
`add(..., is_phantom=False)`.

**4.2 `CodexQueueTracker`** — new `harnesses/codex/queue_tracker.py`, mirror of
`ClaudeQueueTracker`, wrapping one `QueuedSet`:

| signal | action |
|---|---|
| `queued_input` (queued_id, content, ts) | `queued_set.add(queued_id, content, ts, is_phantom=False)` |
| `queued_committed` (queued_id) | `queued_set.resolve(queued_id)` |
| `queued_retracted` (queued_id) | `queued_set.resolve(queued_id)` |

Plus `snapshot()`, `concatenated_block()`, `clear()`, `on_idle()` (→ clear), and
`reset()` (→ fresh set), exactly like `ClaudeQueueTracker`. Resolution is **by id**
— exact, content-free, correct for duplicates, no positional assumption.

**4.3 `parse_codex_queue_signals(line)`** — new, in `codex/session_parser.py`,
mirroring Claude's `parse_queue_signals`: parse one `queued_input.jsonl` line into
a codex `QueueSignal { kind: ENQUEUE | COMMITTED | RETRACTED, queued_id,
content?, timestamp }`, or `None` for a malformed/blank line. Keep it codex-local
(the parsers stay apart; only `QueuedSet` + the state machine are shared — per
§4.5 of the contract). Guard: `queued_id` must be a non-empty str; for ENQUEUE,
content must be a non-blank str.

**4.4 Wire `CodexSessionWatcher`** (mirror the Claude watcher):
- Hold `self._queue_tracker = CodexQueueTracker.build()`.
- In the sidecar-tail path, for each new line: `sig = parse_codex_queue_signals(line);
  if sig: self._queue_tracker.consume(sig)`. After consuming the batch, push the
  snapshot through the registered `QueueSnapshotCallback` if it changed.
- Override `get_queued_messages` / `get_queued_block` / `clear_queue` /
  `notify_idle` / `set_queue_snapshot_callback`, all backed by `_queue_tracker`
  (copy the Claude watcher's implementations verbatim — they are harness-agnostic).
- `reset()` the tracker on session rotation (new rollout / thread switch). The
  fork already emits `queued_retracted` for live entries on resume (self-cleaning),
  so this is a secondary safety net.
- Remove the deleted imports/constants.

## 5. What stays unchanged

`session_watcher.py` interface, `agent_manager` (caching/broadcast + idle
backstop), `models.QueuedMessageState`, the WS `queued_messages` field, the
`/flush-queue` and `/drain-to-composer` endpoints, and the **entire frontend**.
Codex slots in behind the same interface; zero changes there.

## 6. Correctness notes

- **By-id resolution, no content-matching anywhere.** The whole reason for the
  fork work.
- **Self-correcting on reader restart.** The sidecar persists on disk; a fresh
  watcher replays the whole ledger (enqueue + committed/retracted) → nets to the
  currently-pending set. No cursor needed (same conservation property as Claude).
- **Self-cleaning on codex resume.** `restore_thread_input_state` emits
  `queued_retracted` for live entries → the tracker resolves them → snapshot
  empties. The rotation `reset()` is belt-and-suspenders.
- **Flush restart.** `/flush-queue` restarts the agent → new rollout (rotation) →
  `reset()` clears; the fork also retracts live entries on the resume. Either way
  the queued group clears, matching Claude.
- **Backstop.** `on_idle()` → clear on working→IDLE, same as Claude (codex also
  has `task_complete` turn markers, but the ledger + rotation already cover it).
- **Committed turn rendering.** The drained rollout `user_message` renders as an
  ordinary committed turn via the existing `parse_lines` path — no placeholder, no
  double-render.

## 7. Tests

- `harnesses/queued_set_test.py`: add a `resolve(queued_id)` case (removes the
  named entry; no-op if absent; leaves others).
- `harnesses/codex/queue_tracker_test.py` (new): `queued_input`→`queued_committed`
  nets empty; `queued_input`→`queued_retracted` nets empty; two enqueues then
  resolve ONE by id → the **other** remains (by-id correctness, incl. duplicate
  content); full-replay of a recorded ledger nets to the pending set; `on_idle`
  clears; `reset` clears.
- Delete the old codex queued tests (the `queued_input_event` /
  `_dedup_queued_turn` / content-match cases).
- **Fixture caveat:** no codex plugin agent has run on this host, so there is no
  real `queued_input.jsonl` to capture. Either run a codex agent to record one, or
  hand-author a fixture from the documented record shapes (§1) — the shapes are
  fixed by the fork's own `queued_input_log` tests.

## 8. Build order

1. `QueuedSet.resolve(queued_id)` + unit test. (Pure addition; Claude unaffected.)
2. `parse_codex_queue_signals` + `CodexQueueTracker` + tests. First codex-specific
   code; no wiring yet.
3. Wire `CodexSessionWatcher`: feed the tracker, override the queue methods, reset
   on rotation, push snapshots.
4. Tear out `_consume_queued_input` / `_dedup_queued_turn` / `queued_input_event`
   / `normalize_user_content` and dead imports.
5. Full test suite.

## 9. Verification (live — required before shipping)

Must be validated against a REAL codex agent (none has run on this host). Boot a
codex agent, then confirm end to end: queue a message mid-turn → it appears in the
queued group (WS snapshot, not a transcript placeholder); let it commit → the
group entry clears **by id** and the message renders once as a committed turn (no
double-render, no content-match); interrupt → `queued_retracted` clears it;
shoulder-tap (flush) and Stop (drain-to-composer) behave exactly as on Claude.
