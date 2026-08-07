# Shoulder tap — spec (Claude primary, codex delta)

How the system interface renders a message the user sends while an agent is
mid-turn, and the single action ("shoulder tap") that flushes the queue.

Design principle: **the queued state comes only from the harness, never from the
click.** No optimistic bubble. Robust confirmation, not a guess — and when the
confirmation path is fast, it reads as instant anyway.

---

## 1. Goal and non-goals

**Goal.** Exactly two user-visible states for a sent message:

- **queued** — the harness has parked it; it has not been acted on. Rendered as a
  distinct group below the conversation, with one **[Shoulder tap]** button above
  the whole group.
- **sent** — it is an ordinary turn in the conversation.

The [Shoulder tap] button does one thing: **interrupt the agent (restart) and
resend everything currently in the queue as a single combined turn.** No Esc
(rejected — its meaning differs per harness and is unpredictable).

**Non-goals.** No optimistic UI. No client-side content-matching. No per-message
queue actions. No durability — queued state answers "what is happening right
now"; the durable record of a message is the turn it becomes. No queue
manipulation (we observe the harness queue; we do not reorder or dedupe it).

---

## 2. What we are replacing

Today three notions of "queued" overlap:

1. **Optimistic client store** (`frontend/src/models/PendingMessages.ts`,
   `frontend/src/views/PendingMessageView.ts`) — a bubble painted on send-click
   with `sending`/`queued` statuses, reconciled to the transcript by
   whitespace-normalized **content matching**. Its own code carries a FIXME: two
   identical messages are indistinguishable, and a message whose delivered text
   diverges (edited in the terminal, slash-command expansion) strands forever.
2. **Props holding it up** — a forced "Thinking…" activity override, a
   working→IDLE safeguard (`clearQueuedMessagesOnIdle`) that sweeps up bubbles
   the matcher failed to resolve, and a per-message "interrupt and send" button
   (`interruptAndResend`) that handles only one message.
3. **The real harness event** — both harnesses already surface the queued
   message (codex `queued_input.jsonl` sidecar; Claude `queued_command`
   attachment), which the optimistic layer duplicates.

All of #1 and #2 is deleted. #3 becomes the single source of truth.

---

## 3. The model

A message the user sends while the agent cannot start a turn for it is a **tap**.
Internally it has one of these states; only two are user-visible.

```
   (send accepted into harness queue)  ->  QUEUED   (visible: queued group)
   QUEUED --(injected as a turn)------->  COMMITTED (visible: normal turn)
   QUEUED --(pulled back / dropped)---->  GONE      (not shown, no bubble left)
   (send never accepted)--------------->  GONE      (POST surfaces an error)
```

Rules:

1. **A tap becomes QUEUED only from an observed harness event**, never from the
   click.
2. **Every QUEUED tap reaches a terminal state** (COMMITTED or GONE). If a
   renderer can leave one QUEUED forever, it is wrong (§7 backstops).
3. **COMMITTED is proved by the conversation**, not the queue log: the queue log
   says a message left the queue; only the transcript says it arrived as a turn.

Because there is no optimism, RETRACTED and REJECTED both collapse to GONE —
nothing was ever painted, so nothing needs taking down.

---

## 4. Architecture: two channels

Queued state is **ephemeral live state**, like the activity dot — not a durable
transcript event. Keep the two apart.

- **Transcript event stream** (existing SSE `/api/agents/:id/stream` + REST
  `/events`): committed turns only. A queued message enters here the moment it
  commits, as an ordinary `user_message` — no new event type, no tombstones.
- **Live queued-set** (new field on the existing per-agent WebSocket state push,
  alongside `activity_state`): the list of currently-queued messages for the
  agent:

  ```
  queued_messages: [ { queued_id, content, timestamp }, ... ]   // enqueue order
  ```

  The frontend renders the queued group and the [Shoulder tap] button from this
  list. When a message commits, it leaves the set (list update) and appears in
  the transcript. When retracted/dropped, it just leaves the set.

Why a side channel rather than putting queued messages in the transcript stream
with a `queue_state` flag: the transcript is append-only and durable, so a
retraction would need a tombstone and a commit would need in-place supersession,
and the queued group would be entangled with transcript paging/virtualization.
The live-set models "right now" directly, makes the resend trivial (the backend
already holds the set), and removes content-matching from the render path. The
one cost — a brief double-render if the committed transcript event beats the
queued-set drop — is cosmetic and self-heals on the next push.

> Alternative if a side channel is unwanted: keep queued messages as
> `user_message` events carrying `queue_state: "queued"`, flip to committed by
> re-emitting the same `event_id` with the flag cleared (supersession, which the
> codex watcher already does), and drop a retracted one via a hidden tombstone
> event. Reuses more plumbing; costs a tombstone concept and couples the queued
> display to transcript paging. Recommended only if adding a WS field is harder
> than expected.

---

## 5. Claude (primary)

> This section is rewritten from MEASURED data — four real session JSONLs on
> this host, Claude Code **2.1.207** (counts: enqueue 39, remove 27, dequeue 8,
> popAll 4). Earlier drafts guessed the lifecycle; the truth below differs in
> two important ways: `remove` is *inline delivery*, not retraction, and there
> are FOUR operations, not three.

### 5.1 What Claude actually emits

Both mechanisms exist and are **complementary, not alternatives.** They are
written at different moments and describe different halves of the lifecycle.

**`queue-operation` records** — out-of-band sidecar log lines in the session
JSONL (no `uuid`/`parentUuid`/`promptId` — not nodes in the message DAG). Four
operations, obeying a conservation law observed exactly in the data:

```
enqueue = dequeue + remove + popAll     (39 = 8 + 27 + 4)
```

Every enqueued message leaves the queue through exactly one of the other three.

```json
{"type":"queue-operation","operation":"enqueue","timestamp":"2026-08-07T06:33:06.983Z","sessionId":"…","content":"<text>"}
{"type":"queue-operation","operation":"remove","timestamp":"…","sessionId":"…","content":"<text>"}
{"type":"queue-operation","operation":"dequeue","timestamp":"…","sessionId":"…"}
{"type":"queue-operation","operation":"popAll","timestamp":"…","sessionId":"…","content":"<text>"}
```

- **enqueue** — carries `content`. The message is now parked. *This is the only
  on-disk evidence while a message is genuinely still queued.*
- **remove** — carries `content`; means the message was **consumed into the
  currently-running turn** (Claude's native mid-turn shoulder-tap). 1:1 with a
  `queued_command` attachment (below). NOT a user cancel.
- **dequeue** — carries no `content`; the message becomes a **new committed
  `user` turn** after the current one, tagged `promptSource:"queued"`.
- **popAll** — carries `content`, one record per message; a bulk flush of the
  whole queue (e.g. several slash-commands entered at once).

**`queued_command` attachment** — a real DAG node parented into the in-flight
turn, written at `remove` time but stamping the *enqueue* timestamp:

```json
{"type":"attachment","uuid":"…","parentUuid":"…","timestamp":"<enqueue-ts>",
 "attachment":{"type":"queued_command","prompt":"<text>","commandMode":"prompt","origin":{"kind":"human"},"timestamp":"<enqueue-ts>"}}
```

`commandMode` is `prompt` (user text) or `task-notification` (background notice —
skip). This is the **committed, inline-delivered form** of a `remove`. The
current parser (`claude/session_parser.py:527`) already turns it into a
`user_message`, and a `dequeue`'d message already arrives as a normal `user`
record — so **both committed forms already render today**. What is missing is the
*queued* (pre-resolution) state.

### 5.2 Correlation — no id, but a strong key

No record carries any identifier. But the correlator is stronger than plain
content: **`(content, enqueue-timestamp)`**. The enqueue record, its
`queued_command` attachment, and its `remove` all share the same enqueue
timestamp and the same content, so a duplicate message only collides if queued
twice in the same millisecond (not a real case). `dequeue` carries neither, so
dequeue→committed-turn is matched positionally (file adjacency, ~8ms apart) plus
the following turn's `promptSource:"queued"` marker.

### 5.3 Mapping to the model

Adapter tails the session JSONL, keyed on `sessionId`. Pure function from records
to queued-set mutations; holds no UI state. Mint a synthetic `queued_id` =
hash of `(sessionId, enqueue-timestamp, content)`.

| record | action |
|---|---|
| `enqueue` | QUEUED — add `{queued_id, content, timestamp}` to the queued-set |
| `remove` OR its `queued_command` attachment | COMMITTED (inline) — resolve the matching queued entry by `(content, enqueue-ts)`; the attachment already renders as the turn |
| `dequeue` + following `user`(`promptSource:"queued"`) | COMMITTED (new turn) — resolve the head of the remaining queued set positionally; the `user` record already renders |
| `popAll` | COMMITTED (bulk) — resolve each matching entry by content |

There is **no observed true-retract** on the web-UI path (the user has no TUI
affordance to delete a queued item; every `remove` was an inline commit). GONE is
therefore restart-driven only (§5.5). If a future Claude adds a real cancel it
will surface as a `remove` with no accompanying `queued_command` — handle by the
same resolve-and-drop, which is already correct.

### 5.4 The short-window caveat (matters for the button)

remove(27) ≫ dequeue(8): **most queued messages are consumed mid-turn within
seconds**, at the next tool boundary, without waiting for a new turn. So for
Claude the QUEUED window is typically brief, and the [Shoulder tap] button often
races Claude's own auto-consumption — by the time the user clicks, the message
may already be delivered. The button still has value (a long stretch with no tool
call; forcing immediate handling), but it is *less* essential on Claude than on
codex. Do not design as if messages sit queued indefinitely on Claude.

### 5.5 Restart / resume

Claude's live queue is in-memory and dies on restart; the `queue-operation` and
`queued_command` records, however, **persist in the session JSONL and are
present after `claude --resume`** (same file, appended). So a backend re-reading
from offset 0 would re-derive stale queued state. Rule: the queued-set is
**live-only** — start empty on (re)attach and populate only from enqueues whose
resolution has not also been seen; an enqueue in replayed history that already
has its matching remove/dequeue/popAll is resolved and shows nothing. The
working→IDLE transition is a coarse backstop: at a genuine IDLE the queue is
drained, so drop anything still QUEUED (§7).

---

## 6. codex (how it replicates)

codex maps onto the same model; only the plumbing differs. Existing code:
`codex/watcher.py` (`_consume_queued_input`, `_dedup_queued_turn`),
`codex/session_parser.py` (`queued_input_event`).

> MEASURED caveats (from this host): (1) **No codex plugin agent has ever run
> here**, so there is no live sidecar to observe — codex behavior below is from
> code + tests, not live data; validate against a real codex agent before
> shipping. (2) The `client_id` correlation patch is **NOT present** in the
> pinned 0.146.0 binary — the one real rollout has zero `client_id` fields and
> shows the mid-turn drain pattern (two `user_message`s 4ms apart) with no
> correlation id. So codex commit correlation is content-based today, full stop.

| step | Claude | codex |
|---|---|---|
| enqueue | `queue-operation/enqueue` → queued-set | `$CODEX_HOME/queued_input.jsonl` line → queued-set, keyed by the sidecar's stable `queued_id` |
| commit | `dequeue` + following `user` record | drained rollout `user_message` → transcript turn; drop from queued-set, correlated by content today (`_dedup_queued_turn`) or by `client_id` if the codex binary patch lands |
| retract | `queue-operation/remove` | **no record emitted** → §7 backstops |
| turn boundary | none | `task_started` / `task_complete` in the rollout (real-time) |
| correlation id | none (positional FIFO) | `queued_id`, stable — the mirror image: codex has an id and (today) no lifecycle; Claude has a lifecycle and no id |

codex specifics:

- **enqueue**: the sidecar is written the instant a message is queued and carries
  a stable `queued_id` — use it as the queued-set key (no minting needed).
- **commit**: when the steer injects at a tool-call boundary, the rollout writes
  the `user_message`. `_dedup_queued_turn` matches it to the placeholder by
  normalized content (brittle for duplicates — the same reason the placeholder
  uses `queued_id`). If the codex binary is patched to thread `queued_id` into
  `client_user_message_id` (external spec §4), the committed rollout turn carries
  the id verbatim and commit becomes a key lookup instead of a text search.
- **retract**: codex emits nothing when steers drain back to the composer (Esc,
  Ctrl-C, budget abort, `ActiveTurnNotSteerable`, a safety re-fork under a new
  `thread_id`). §7's turn-end rule covers all of these.
- **turn boundary**: because codex records `task_complete`, the turn-end backstop
  (§7) is implementable directly for codex; Claude leans on `remove` + the
  working→IDLE transition instead.
- **restart/resume**: CORRECTED — the sidecar **does persist**. It lives at
  `<agent_state_dir>/plugin/codex/home/queued_input.jsonl`, is per-agent (not
  per-session), is never truncated, and is **not** in the plugin's launch-time
  `reset_marker_cmd` cleanup list, so it survives `mngr restart` / `codex
  resume` with all history intact. The real danger is therefore a *reader*
  restart: a fresh watcher resets `_queued_offset` to 0 and re-reads the whole
  sidecar, re-surfacing already-committed or dropped messages as stale QUEUED
  bubbles. Fix: persist a **sidecar byte high-water mark** per agent (see §8.5
  persistence) and never re-emit below it; on rollout rotation clear the live
  queued-set. Committed ones are also rescued by content-dedup against the
  re-read rollout, but a never-drained (retracted) one has nothing to dedup
  against — the high-water mark is what actually closes this hole.
- **merge caveat**: `on_interrupted_turn` can join several parked steers into one
  `\n`-separated turn, so N QUEUED taps resolve to one COMMITTED turn. With
  `queued_id`s this is explicit (one `client_id` on the turn, the others never
  appear) and §7 cleans up the rest. Measured live, steers injected at
  successive tool boundaries produced separate turns; the merge needs Esc while
  ≥2 are parked — not a path a system-interface user hits, but the backstop makes
  it harmless.
- codex has three input queues; the sidecar observes `pending_steers`, the
  shoulder-tap queue and the only one relevant here. `queued_user_messages`
  (boot / `!shell` / plan mode / popup) and `rejected_steers_queue` are off the
  web-UI path; ignore them unless completeness is wanted later.

---

## 7. Terminal-state backstops (correctness, not fallback)

Every QUEUED tap must reach a terminal state. Three rules guarantee it:

1. **Commit** — observed as above (dequeue+user for Claude; drained rollout turn
   for codex) → leaves the queued-set.
2. **Explicit retract** — Claude `remove`; codex has none today.
3. **Turn-end reconciliation** — when a turn ends, any tap still QUEUED that has
   not appeared in the transcript is GONE. This single rule covers every codex
   hole (all the no-record retractions above) and any missed Claude signal. For
   codex the trigger is `task_complete`; for Claude the working→IDLE transition
   (§5.5). A restart/resume is the coarsest version: clear the whole set (§5.3).

---

## 8. The shoulder-tap action

One button above the queued group. Both variants below share steps 1–3 (a new
server-side atomic endpoint, e.g. `POST /api/agents/:id/flush-queue`); they
differ only in what happens after the restart. **Recommendation:
flush-to-composer (§8.2)** — it is more robust, gives the user an edit moment,
and dissolves the "combining flattens intent" objection. Auto-resend (§8.1)
stays documented as the one-click alternative.

Shared prefix:

1. Read the agent's current queued-set (backend holds it; §8.5 makes it survive
   a backend restart). If empty, no-op.
2. `mngr start <agent> --restart --no-resume`. Verified semantics: kills and
   relaunches; **conversation history is preserved** (each harness resumes its
   own on-disk session); in-harness queued messages are dropped by the SIGKILL
   (why we must re-deliver); `--no-resume` only suppresses an optional stored
   resume prompt; the agent returns idle in a few seconds. Refused only for
   `is_primary=true` (the workspace services agent) — the coding agents the user
   chats with are not primary, so this does not restrict us.
3. Mark the flushed entries resolved in the durable cursor (§8.5) so a
   subsequent re-read does not resurface them, and clear the live queued-set.

### 8.1 Variant A — auto-resend combined (one click)

4a. Re-deliver the queued contents **as one combined message**, joined by
    newlines, in enqueue order, through the existing `send_message` path (waits
    for TUI ready after restart; confirms submission against durable on-disk
    evidence, STRICT). Combining is required, not cosmetic: after restart the
    agent is idle, so sending them one at a time would let the first open a turn
    and the rest re-queue — defeating the flush.

Cost: commits immediately and flattens N messages into one turn.

### 8.2 Variant B — flush back to the composer (recommended)

4b. Do **not** re-deliver. Instead put the combined queued text (newline-joined,
    enqueue order) back into the **web composer**, editable, and let the user
    review and send. The queued group clears; the agent sits idle with history
    intact.

Why this is better:

- **Robust and honest** — the text returns to the user's hands (spec §3's
  RETRACTED semantics). No auto-commit, no guessing, nothing to reconcile.
- **Solves merge-intent** — the user can edit, split, or drop before sending, so
  combining is no longer a lossy decision the system makes for them.
- **Cheap** — the frontend already has the queued texts (WS list) and already has
  a composer-prefill path: the send-failure handler in `MessageInput.ts:226-237`
  writes `localStorage[messageTextKey(agentId)]` + sets `messageText` and
  redraws, guarded by an `isComposerEmpty` check. Reuse that verbatim (prepend,
  or guard, if the composer is non-empty).
- Prefer prefilling the **web** composer over the tmux pane. If a backend-side
  prefill is ever wanted, the primitive exists: `_send_tmux_literal_keys`
  (`mngr/agents/base_agent.py:709`) types into the pane **without** sending
  Enter — a "type, don't submit" method is that call minus
  `submit_message_and_confirm`.

Both variants restart the agent, so both **interrupt the current turn** — that is
inherent to pulling messages out of the harness queue (the only harness-agnostic
way to clear it, since Esc is rejected). "Shoulder tap" therefore means "stop and
take my input now," which is the intended semantic. On Claude, mind the §5.4 race:
the message may already have been auto-consumed before the click.

### 8.5 Persisting the queued-set across a backend restart

The spec's default is memory-only (durability is a stated non-goal). The
investigations surfaced one real hole worth closing cheaply: because the harness
queue files persist on disk (codex sidecar is never truncated; Claude
`queue-operation` records stay in the JSONL), a *backend* restart re-reads them
and can resurface already-handled messages as fresh QUEUED taps — and after a
flush-restart, the just-killed messages are still in those files.

Do **not** duplicate the message contents into a backend store. Instead persist
only a small per-agent **reconciliation cursor** (low-churn — written on resolve
and on flush, not per keystroke), e.g.
`<agent_state_dir>/plugin/system_interface/queue_cursor.json`:

- codex: the sidecar **byte high-water mark** already handled.
- Claude: the index/timestamp of the last resolved `enqueue`.

On boot, the backend re-derives the live queued-set from the harness files
*above the cursor* only. The harness files remain the source of truth for
content; the cursor is the only thing the backend must own durably. This fixes
both the reader-restart staleness (§5.5, codex restart bullet) and the
post-flush resurface, without adopting `data.json` (whole-file atomic rewrites +
external-storage push on every mutation — wrong for churn) and without a
content-bearing sidecar of our own.

**Invariant (any storage choice):** "a message that already committed must not be
re-delivered" is decidable only against the transcript — so the durable cursor is
an optimization, never the authority; reconcile against the live conversation at
flush time (spec §3 rule 3).

---

## 9. Frontend changes

**Tear out**

- Delete `frontend/src/models/PendingMessages.ts` and
  `frontend/src/views/PendingMessageView.ts`.
- `MessageInput.handleSend()`: POST and return. No optimistic bubble, no
  `sending`/`queued` client status, no rollback dance.
- Remove the forced-THINKING override, `getEffectiveActivityState`,
  `initQueuedMessageIdleClearing`, `clearQueuedMessagesOnIdle`, and every
  `reconcilePendingMessages` call site (`Response.ts`, `ChatPanel.ts`).

**Add**

- Consume `queued_messages` from the per-agent WS state push.
- Render the queued group below the last committed turn, in enqueue order,
  visually distinct from committed messages, with one [Shoulder tap] button above
  the group. Reuse the user-bubble *view* (not the classifier — queued messages
  never call `classifyUserMessage`; see §13). Button → `POST /flush-queue`;
  disable while in flight.
- Recommended button behavior (§8.2): on success, prefill the composer with the
  combined queued text (reuse the `MessageInput.ts:226` localStorage path) and
  clear the queued group. The user reviews and sends.
- Nothing paints on send; the queued bubble appears when the harness records the
  enqueue (typically ~1s). A normal (non-queued) message appears when its turn
  lands. If perceived latency is ever a problem, re-introduce a *distinct*
  transient "sending" state with a timeout that resolves to an error — but never
  bring back content-matching.

---

## 10. Edge cases

- **Normal send to an idle agent** — not queued; appears as its turn when the
  transcript lands. POST + STRICT confirmation is the feedback.
- **Auto-commit before the tap** — a queued message injects on its own (codex
  tool boundary; Claude dequeue) → leaves the set, becomes a turn; the button
  now has fewer/zero to resend.
- **Duplicate content** — indistinguishable under content-matching, distinct
  under ids. Claude has no ids (positional FIFO handles it within a session);
  codex has `queued_id`. Justifies the codex binary patch if duplicates bite.
- **Restart from elsewhere / crash / OOM** — treated as §5.3: queued-set cleared
  on resume/rotation.
- **Send never accepted** — the POST errors; nothing was painted, so nothing to
  clean up.

---

## 11. Build order

1. **Backend queued-set + terminal backstops.** Claude adapter on the four
   `queue-operation` ops (§5.3), codex adapter reusing the sidecar reader plus the
   §8.5 high-water cursor; expose `queued_messages` on the per-agent WS state.
   Assert against recorded fixtures — real ones already exist on this host for
   Claude; capture codex fixtures from a live codex agent first (§12.6). No UI
   change — safe, reversible.
2. **Backend `/flush-queue`** — restart + (chosen variant §8.1/§8.2) + cursor
   update.
3. **Frontend tear-out + queued group + button** — recommended: button prefills
   the composer (§8.2).

Rough size: backend ~half a day, frontend tear-out ~half a day, button small.
Real Claude fixtures are in hand; the main remaining unknown is live codex.

---

## 12. Open questions

1. ~~Step 0~~ **SETTLED by measurement.** The container's Claude (2.1.207) emits
   BOTH `queue-operation` (4 ops) and `queued_command`, complementary. Build on
   §5.1–§5.3.
2. **Button behavior** — flush-to-composer (§8.2, recommended) or auto-resend
   combined (§8.1)? The user leaned auto-resend earlier, then raised
   flush-to-composer; §8.2 argues the latter is strictly more robust. Decide.
3. **Persistence** (§8.5) — memory-only (spec non-goal) or the small durable
   cursor that closes the codex reader-restart + post-flush-resurface holes?
   Recommend the cursor; it is cheap and fixes real bugs the investigations
   found.
4. **Side channel vs transcript flag** (§4): recommend the WS live-set; confirm.
5. **codex binary patch** (external spec §4, thread `queued_id` into
   `client_user_message_id`): NOT present in 0.146.0 today. Out of scope for the
   system interface, but the only way to make codex commit-correlation exact for
   duplicates. Do it, or accept content-matching for codex?
6. **codex live validation**: no codex plugin agent has run on this host, so the
   codex adapter must be validated against a real codex agent before shipping.

## 13. UserMessageKind — no change

Queued messages do **not** get a new `UserMessageKind`. That enum disambiguates
the overloaded transcript `user_message` channel; queued state lives on the WS
side channel (§4) and renders through a separate path that reuses the bubble
*view* without calling `classifyUserMessage`. A committed message is an ordinary
turn (`UserPrompt`) — Claude even tags a dequeued one `promptSource:"queued"`,
but nothing visual needs to change. Adding a "committed-from-queue" kind would
require exactly the transcript↔queue correlation this design avoids, for no
visual gain. Backend stamps no queue flag on transcript events.
