# Antigravity (agy) message queuing — design spec

Goal: when a user sends a message to a **busy** agy agent, show it as a "queued" bubble
immediately, then let it become the real turn when agy processes it — the UX Codex gives.
This spec is grounded in live simulations against real agy agents; the findings materially
shaped the design (agy **coalesces** its queue — see §2).

Status: design only, not built. Session-dependent by nature: the queue lives in agy's TUI
memory and dies with the process (a restart/interrupt wipes it), exactly like Codex. That is
acceptable and reflected below.

---

## 0. Why Codex's mechanism doesn't port
Codex's queuing relies on a **patched codex binary** that appends every enqueued message to a
`queued_input.jsonl` sidecar with a stable `queued_id`; its watcher tails that into a
placeholder bubble and re-keys the drained rollout turn onto it (1 queued message → 1 rollout
turn, matched by normalized content). **agy has neither a sidecar nor 1:1 draining.** We must
source the queued signal ourselves, and handle coalescing.

---

## 1. What agy gives us (all verified live)

| Signal | Finding | Use |
|---|---|---|
| **Transcript timing** | A message queued while agy is busy is **not** in the transcript (`steps` DB) until agy processes it; then it lands as an ordinary `USER_INPUT` step with no queued marker. | The transcript is the *drain* signal, not the *queued* signal. |
| **Send acceptance** | `send_message_to_agent` → mngr confirms delivery when agy's `active` marker advances. Sending to a **busy** agent returned success **well before** the turn ended — mngr confirms the message was *accepted into agy's queue* promptly, it does not block to turn-end. | The **primary queued signal**: accepted + agent-was-busy ⇒ queued. |
| **Pane** | Queued messages render as consecutive `▸`-prefixed lines above the input box. | Optional enrichment (§7); fragile. |
| **Interrupt** | The UI "interrupt" is `mngr start --restart --no-resume` — a **process restart**, which wipes agy's in-memory queue and calls `reset_activity_state`. | Clear the outbox on interrupt (§6). |

---

## 2. The decisive finding: agy COALESCES its queue

**When agy finishes its current turn, it joins ALL pending queued messages with `\n` into a
single `USER_INPUT` turn and produces one reply.** Verified live:

- 2 queued → transcript turn `'QUEUE1: reply with only the word ONE\nQUEUE2: reply with only the word TWO'`
- 3 queued → transcript turn `'AAA say a\nBBB say b\nCCC say c'`

The pane even shows them stacked under one `>` turn, and agy's own summary said "Processed
both queued items … within a single turn." So the mapping is **N queued messages → 1 combined
turn**, not Codex's 1→1. This is the core constraint the design must handle: a drained turn
must reconcile against **multiple** outbox entries at once, and a single queued message is just
the N=1 case (no `\n`).

---

## 3. Design — a backend-authoritative outbox (not the frontend optimistic store)

Keep the queued state in a **backend per-agent outbox** that the backend fills from the send
signal and drains by watching the transcript. The frontend only *renders* the outbox list; it
does not run its own optimistic send/reconcile (the brittle `PendingMessages` content-reconcile
path we're avoiding). Because the outbox is a plain list keyed by content, coalescing is handled
by clearing several entries against one drained turn — no per-event id-superseding gymnastics.

### Components
- **`AntigravityOutbox`** (new, per agent): an ordered list of entries
  `{queued_id, content, normalized_content, sent_at}`. `queued_id` is stable
  (`f"agy-queued-{sha1(normalized+ordinal)}"`).
- **`AgentManager.send_message_to_agent`** (existing): after a successful send, if the agent's
  `activity_state` was **not IDLE** (busy ⇒ queued), append to the outbox and broadcast.
- **`AntigravitySessionWatcher`** (existing): on each new `USER_INPUT` event, run the
  **drain-reconcile** (§4) to remove the matched outbox entries and broadcast.
- **Serialization**: the agent's serialized state gains `queued_messages: [{queued_id, content}]`
  (ordered). It rides the existing `agents_updated` WebSocket broadcast, so no new stream.
- **Frontend**: renders `queued_messages` as "Queued" bubbles appended after the transcript,
  before the input box. No optimistic store, no content-reconcile — the backend owns the list.

### Flow
1. **Send.** UI POST `/message` → `send_message_to_agent`. On success, read `activity_state`:
   - **busy** (THINKING / TOOL_RUNNING) → append an outbox entry → broadcast → queued bubble shows.
   - **idle** → no entry; the message is processed now and appears in the transcript within ~1s.
2. **Queued bubbles** render from `queued_messages` (ordered, styled as pending).
3. **Drain.** agy coalesces + processes → the watcher decodes one combined `USER_INPUT` turn.
4. **Reconcile (§4).** The combined turn's `\n`-segments are matched against the front of the
   outbox; matched entries are removed → broadcast → queued bubbles disappear; the combined turn
   now renders from the transcript (see §5 for the 3→1 collapse this implies).

---

## 4. Drain-reconcile (handles coalescing)

On each newly-emitted `USER_INPUT` event (normalized content `C`):
1. If the outbox is empty → nothing to do (normal message).
2. Split `C` on `\n` into ordered segments `s1..sk` (normalize each).
3. Walk the outbox from the front; if `s1..sk` equal the normalized `content` of the first `k`
   outbox entries **in order**, remove those `k` entries (they've drained). This covers:
   - **coalesced** drain (`k = N` queued messages joined), and
   - **single** queued message (`k = 1`, no `\n`).
4. If the segments don't line up with the front run (e.g. the user also typed directly in the
   terminal, or agy reformatted), fall back to a **guarded fuzzy match**: for each segment,
   remove the single outbox entry whose normalized content it best matches on the last-64-char
   suffix; only reconcile when exactly one candidate matches (never mis-pair two similar
   messages). Leave unmatched entries in the outbox (they'll clear on the never-drains safety
   net, §6).

Matching primary key is **exact normalized content** because the parser already strips agy's
`<USER_REQUEST>…</USER_REQUEST>` wrapper, so the drained text equals the sent text — no fuzzy
needed for the common case; the fuzzy pass is only a safety fallback.

---

## 5. Rendering: the 3→1 collapse (accepted)
Because agy coalesces, the durable transcript holds **one** combined turn, while the outbox held
**N** queued bubbles. On drain the N queued bubbles are removed and the one combined turn renders
(its literal `\n`-joined text, as one user bubble), followed by agy's single reply. So three
queued bubbles visibly **collapse into one** combined bubble on drain.

This is accepted, because it is honest (agy really did process them as one turn) and it is
**consistent with a rebuild-from-transcript** (a page refresh / reconnect, or a backend restart,
shows the same one combined bubble — there is no durable N-bubble form to preserve). Splitting the
combined turn back into N bubbles was rejected: it can't be distinguished from a genuine
multi-line single message, and it would still collapse on any transcript rebuild.

---

## 6. Edge cases
- **Sent while IDLE** → not queued; no outbox entry (message appears in the transcript promptly).
- **Interrupt** = process restart (verified): agy's in-memory queue is gone. Hook the outbox clear
  into `AgentManager.reset_activity_state` (the interrupt path already calls it) → drop all entries
  for that agent. No synthetic marker needed.
- **Never-drains safety net.** If the lifecycle returns to a settled IDLE (turn + queue fully done)
  and outbox entries remain unreconciled, drop them — they provably won't arrive (agy drained its
  queue). Mirrors the frontend's working→IDLE queue-drain safeguard, on the backend.
- **Backend restart.** The outbox is in-memory and lost on restart; the still-queued messages are
  gone from *our* view too (and, since interrupt=restart is how they'd be cleared anyway, the agy
  queue may also be gone). Drained messages still render from the transcript. No persistence —
  matches the session-dependent nature the user accepts.
- **Terminal-typed messages** (not via the UI) never enter the outbox (no send went through us);
  they render only on drain, as the combined turn. §7 is the only way to show them pre-drain.
- **Duplicate content** queued twice → two entries with distinct `queued_id` (ordinal suffix);
  the front-run match removes them in order.

---

## 7. Optional enrichment — pane cross-check (defer)
To also show queued bubbles for **terminal-typed** messages, or to detect a **dropped** queue,
add a periodic `tmux capture-pane`: take the contiguous run of `▸`-prefixed lines immediately
above the input-box separator (excludes the "Thought for Ns" `▸`, which sits by the activity
output, not the input box), de-wrap, and seed/verify the outbox by normalized content. A queued
line that vanishes without a transcript drain was dropped → remove its entry. Brittle (wrapping,
width, `▸` ambiguity, scrollback, per-cycle tmux cost); ship §3 first, add this only if
terminal-typed queuing matters.

---

## 8. Build order
1. `AntigravityOutbox` (ordered list + normalized index + front-run/fuzzy match) + unit tests
   (coalesced drain clears N entries; single clears 1; fuzzy fallback; safety-net clear).
2. Serialize `queued_messages` onto the agent state; broadcast on change.
3. `send_message_to_agent`: on success + busy → append; wire `reset_activity_state` → clear.
4. Watcher: run drain-reconcile on each new `USER_INPUT` event, clearing matched entries.
5. Frontend: render `queued_messages` as queued bubbles after the transcript (a `queued` style
   pill); no optimistic store.
6. Manual tmux verification: busy agent, send 3 via the UI → 3 queued bubbles; on drain they
   collapse into the one combined turn + reply; interrupt mid-queue → bubbles clear.

---

## 9. Limitations (explicit)
- **Session-dependent / in-memory** — queue dies with the agy process (restart/interrupt) and the
  outbox dies with the backend. No persistence, by design (matches Codex).
- **N→1 coalescing collapse** — N queued bubbles become one combined bubble on drain (§5).
- **UI-sent only** — terminal-typed queued messages have no pre-drain bubble unless §7 is added.
- **Content-based reconcile** — exact-normalized primary + guarded fuzzy fallback; imperfect if agy
  reformats the text, same brittleness class Codex accepts.
