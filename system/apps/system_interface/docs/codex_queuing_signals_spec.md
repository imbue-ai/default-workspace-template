# Codex queuing signals — spec for Claude-analogous wiring

What the `codex-in-minds` fork must emit so a `CodexQueueTracker` can populate the
**same** `QueuedSet` entity as Claude, with **zero content-matching** — i.e. codex
wired symmetrically to Claude. Companion to `claude_queued_messages_impl.md` (the
Claude side, now shipped) and `shoulder_tap_spec.md` §4.5 (the cross-harness
contract). Grounded in a read of the fork's `patches/0.146.0.patch`.

## 1. Verdict up front

**Not sufficient today.** The fork emits an **enqueue-only** signal and **no leave
signal**. So a symmetric, content-match-free populator is impossible as-is: the
tracker could `add` but never `resolve`, and the FIFO would grow forever from
harness signals. Two small additions fix it.

This is the exact mirror of Claude: **Claude has the lifecycle (leave-ops) but no
id; codex has an id but no lifecycle.** Codex needs the leave side.

## 2. The point, and how Claude does it (self-contained — read this first)

**The point.** When a user sends a message while the agent is mid-turn, the
harness *queues* it. Minds must show that queued message in the UI and offer to
act on it. The design rule is: **the frontend is dumb and invents no state — it
renders exactly what the harness reports.** "Queued" is not a guess the UI paints
on send; it is a fact read out of the harness's own queue. Everything hard about
this feature is getting a *reliable, harness-sourced* queued state without
fragile heuristics. Codex must feed that same machine.

**Why not just content-match?** The tempting shortcut — watch the message get
queued, then match its text against the turn it later becomes — is what the whole
design rejects. Text matching can't tell two identical messages apart, breaks when
the text is transformed (slash-command expansion, edits), and can't represent a
*retracted* message at all. The Minds side had exactly this and it stranded/duped
messages. So resolution must be by an explicit signal, never by text.

**How Claude does it (the model codex must match).** Claude writes a small
out-of-band **ledger** to its session log. Every queued message produces an
`enqueue` record; every message that *leaves* the queue produces exactly one of
`dequeue` (it opened its own turn), `remove` (it was injected inline into the
running turn), or `popAll` (the whole queue was flushed). These obey a
conservation law:

```
enqueue = dequeue + remove + popAll        (every enqueue has exactly one leave)
```

A single tracker (`ClaudeQueueTracker`) folds that ledger into a FIFO
(`QueuedSet`):

- `enqueue` → **add** an entry. (Framework noise — Claude's `<task-notification>`
  items ride the same queue — is added as an invisible "phantom" that holds a FIFO
  slot but never shows.)
- any leave record (`dequeue`/`remove`/`popAll`) → **resolve** (drop the FIFO
  head).

No ids, no text matching — purely the ledger. Because every enqueue has exactly
one leave, replaying the whole log nets to precisely the still-pending set (it is
self-correcting; no durable cursor). The set of currently-pending messages is
pushed — as a **full snapshot every time** — on the per-agent WebSocket state,
right beside the activity indicator. The frontend replaces its "queued" group
wholesale on each push and renders it below the conversation.

**Two actions, both harness-agnostic.** Above the queued group is a *Shoulder tap*
button (flush: restart the agent and resend the whole queue as one concatenated
turn); the composer's *Stop* button drains the queue back into the composer
(restart + hand the concatenated block to the input box, unsent). Both read only
`QueuedSet` and the generic restart/send plumbing — **zero harness-specific code.**

**So what codex needs.** The `QueuedSet` entity, the WebSocket snapshot, the two
actions, and the entire frontend are already built and harness-agnostic. Wiring
codex in means writing **one thing**: a `CodexQueueTracker` that folds codex's
queue signals into the same `QueuedSet`. For that tracker to exist without text
matching, codex must emit the same shape of ledger: an **enqueue** signal AND a
**leave** signal per message. It emits the first today and not the second — that
is the entire gap this spec closes.

## 3. What the fork emits today (measured from the patch)

One module, `codex-rs/tui/src/chatwidget/queued_input_log.rs`, one call site.

- **ENQUEUE — yes.** At the `pending_steers.push_back` site in
  `chatwidget/input_submission.rs`, immediately before parking the steer, it
  appends one line to `$CODEX_HOME/queued_input.jsonl`:

  ```json
  {"type":"queued_input","queued_id":"<uuid-v4>","thread_id":"...","timestamp":"...","content":"..."}
  ```

  `queued_id` is a locally-minted `Uuid::new_v4()`, stable per enqueue. This is the
  `pending_steers` queue (the mid-turn shoulder-tap queue) — the correct one.
- **COMMIT — no record.** When a parked steer drains and is injected into the
  turn, nothing is written to the sidecar; the message just appears later in the
  core-written rollout as an ordinary `user_message`, and it does **not** carry the
  `queued_id` (the `client_user_message_id` literals in `app_server_session.rs` are
  still `None` — the threading the fork's own `spec-shoulder-tap.md` §4 proposes is
  not implemented).
- **RETRACT — no record.** Nothing on `on_interrupted_turn` (Esc / drain-back),
  the `rejected_steers_queue` path, or thread-clear / safety re-fork.

Consequence: the only resolution available today is **content-matching** the
pending sidecar text against the drained rollout `user_message` (what
`_dedup_queued_turn` does) — precisely what this design eliminates, and it can't
disambiguate duplicates or resolve a retract at all.

## 4. Required additions (minimal, all TUI-side, mirror `append_queued_input`)

Preserve one **invariant**: exactly ONE terminating record (`committed` OR
`retracted`) per `queued_input`. That is the conservation law, codex-side.

1. **`queued_committed`** — `{"type":"queued_committed","queued_id":"...","timestamp":"..."}`
   (no content — re-emitting content invites disagreement, per the fork's own
   spec). Emit where a parked steer is popped from `pending_steers` and
   injected/submitted to core (the drain/inject site; the counterpart of the
   `push_back` that enqueue records). **Requires threading the enqueue's
   `queued_id` alongside each `PendingSteer`** — today `append_queued_input`
   returns the id but the caller in `input_submission.rs` discards it. Store it on
   the `PendingSteer` so the drain site can name it.

2. **`queued_retracted`** — `{"type":"queued_retracted","queued_id":"...","timestamp":"..."}`,
   emitted at every path that removes a parked steer WITHOUT committing it:
   - `on_interrupted_turn` (Esc / drain-back-to-composer). Note the **merge case**:
     when several steers collapse into one turn, emit `queued_committed` for the
     survivor(s) that were injected and `queued_retracted` for the rest.
   - the `rejected_steers_queue` path (`ActiveTurnNotSteerable`).
   - thread-clear / safety re-fork. `InputQueueState::clear()` must emit one
     `queued_retracted` per surviving entry, or the log leaks un-terminated
     entries.

3. **(Preferred) thread `queued_id` into `client_user_message_id`** — replace the
   two `None` literals in `codex-rs/tui/src/tui/src/app_server_session.rs` so the
   committed rollout `user_message` carries `client_id == queued_id`. This gives an
   authoritative, content-match-free COMMIT correlation that also survives the
   sidecar being non-durable, and lets the populator resolve commits from the
   durable rollout as well as the sidecar. Not strictly required if #1 ships, but
   it hardens COMMIT and is cheap (threading one `Option<String>` every layer below
   already accepts).

> Pin the exact upstream 0.146.0 sites before implementing: the `pending_steers`
> drain/injection function (counterpart of `input_submission.rs`'s `push_back`),
> `on_interrupted_turn`, the `rejected_steers_queue` handler, and
> `InputQueueState::clear()`. The patch itself doesn't touch these, so name them
> against a real codex 0.146.0 checkout.

## 5. The symmetric codex populator (once §4 ships)

`CodexQueueTracker` wrapping one `QueuedSet` — the only codex-specific code, mirror
of `ClaudeQueueTracker`:

| signal | action |
|---|---|
| `queued_input` | `QueuedSet.add(queued_id, content, ts)` |
| `queued_committed` / `queued_retracted` | `QueuedSet.resolve(queued_id)` — **by id**, cleaner than Claude's positional `resolve_oldest` |
| backstop | `task_complete` turn-end + rollout rotation → `clear` (coarse sweep) |

Notes:
- Codex resolves **by id** (the fork mints `queued_id` and echoes it on the leave
  record), which is *stronger* than Claude's positional resolution — no FIFO-order
  assumption, correct for duplicates and out-of-order drains.
- Phantom placeholders (Claude's task-notification case) are likely **unnecessary**
  for codex: `pending_steers` holds user steers only, not framework notices. If the
  fork ever routes non-user items through `pending_steers`, apply the same phantom
  rule (add, hide from snapshot); otherwise every `queued_input` is a real entry.
- Self-correcting on replay: because every `queued_input` gets exactly one
  terminating record, replaying the whole sidecar nets to exactly the still-pending
  set — no durable cursor needed (same property as the Claude ledger). On rollout
  rotation, clear and re-derive.

## 6. Wiring parity checklist (Minds side, after §4)

- `CodexSessionWatcher` already tails `queued_input.jsonl`; extend it to also
  recognize `queued_committed` / `queued_retracted` and feed a
  `CodexQueueTracker`.
- Expose the `queued_messages` snapshot on the per-agent WS state — **same field,
  same shape** as Claude.
- `QueuedSet`, the two endpoints (`/flush-queue`, `/drain-to-composer`), and the
  entire frontend: **zero changes** (harness-agnostic; the litmus test from
  `shoulder_tap_spec.md` §4.5 — no `if harness == …` anywhere on the read/render
  path).
- Add a cross-harness contract test: feed a recorded codex sidecar (enqueue +
  committed/retracted) and assert the resulting `snapshot()` sequence matches the
  Claude adapter's for the same scenarios.

## 7. Build order

1. Fork: thread `queued_id` onto `PendingSteer`; emit `queued_committed` at the
   drain/inject site. (Turns COMMIT into an id lookup.)
2. Fork: emit `queued_retracted` at `on_interrupted_turn`, `rejected_steers_queue`,
   and `clear()`. (Completes the conservation law.)
3. (Optional, hardening) Fork: thread `queued_id` → `client_user_message_id`.
4. Minds: `CodexQueueTracker` + watcher wiring + the parity test. No other Minds
   changes.

Once steps 1–2 land, codex is wired "insanely analogously" to Claude — same
entity, same snapshot, same actions, one thin populator that resolves by id.
