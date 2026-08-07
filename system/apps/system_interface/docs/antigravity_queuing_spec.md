# Antigravity (agy) message queuing — design spec

Goal: show a "queued" bubble in the chat the instant a user sends a message to a **busy**
agy agent, then have it become the real turn seamlessly when agy processes it — the same
UX Codex gives. **Out of the box agy makes this hard**, so this spec picks the signal that
actually works and mirrors Codex's reconciliation.

Status: design only. Not built. Queuing was explicitly deferred in the transcript-harness
cut; this is the follow-on.

---

## 0. Why Codex's approach doesn't port

Codex's queuing (verified in `codex/watcher.py` + `codex/session_parser.py`) relies on a
**patched codex binary** that appends every enqueued message to a sidecar
`queued_input.jsonl` with a stable `queued_id`. The watcher tails that sidecar into a
placeholder `user_message` bubble, and when the message later drains into the rollout it
re-keys the drained turn's `event_id` onto the placeholder's id (matched by
whitespace-normalized content, `_dedup_queued_turn`) so the bubble becomes the real turn
with no double-render.

**agy has no such sidecar and we can't patch it.** Confirmed empirically:
- A message typed while agy is busy is **not** written to its transcript (the `steps` DB)
  while queued; it lands as an ordinary `USER_INPUT` step only once agy dequeues and
  processes it, with no "queued" marker.
- The only place a queued message is visible before processing is **agy's TUI pane**, where
  it renders as a `▸`-prefixed line (in-memory).

So we must source the "queued" signal ourselves.

---

## 1. The two signals agy gives us (both verified live)

### Signal A — the send-acceptance signal (robust, recommended)
The UI sends through `AgentManager.send_message_to_agent` → mngr's `send_message_to_agents`.
For antigravity, mngr confirms delivery when agy's **`active` lifecycle marker advances**
(the statusLine touches it on the accepted keystroke). Verified: sending to a *busy* agy
agent (mid `sleep 40`) returned **success in well under the remaining turn time** — mngr
confirms the message was *accepted into agy's queue* promptly, it does **not** block until
the turn ends. So the backend already knows, per send:
- **the message was accepted** (`send_to_agent` returned True), and
- **whether the agent was busy at that moment** (its `activity_state` != IDLE) → i.e.
  whether it was *queued* vs processed immediately.

And we know it's still queued for as long as it has **not appeared in the transcript**.
That triple — accepted + busy-at-send + not-yet-in-transcript — is a precise "queued"
signal with no fragile parsing.

### Signal B — the pane (authoritative to agy, but fragile)
`tmux capture-pane` shows queued messages as consecutive `▸`-prefixed lines between the
activity spinner and the input box. Verified with three queued messages:
```
⣻  Running...
▸ QMSG_ALPHA please remember alpha
▸ QMSG_BRAVO then bravo
▸ QMSG_CHARLIE finally charlie
────────────────────────────────
>
```
Authoritative to what agy *actually* has queued (regardless of whether the UI or a
terminal-typed message put it there), but fragile: `▸` is also the "Thought for Ns" prefix
(needs positional parsing — the run of `▸` lines immediately above the input box), long
messages wrap across pane lines, width/scrollback vary, and it's an extra poll per cycle.

**Decision: build on Signal A (the outbox); use Signal B only as an optional enrichment
(§6).** A is precise for the UI's own sends — which is exactly the case the UI must render —
and needs no screen-scraping.

---

## 2. Design — a backend outbox, reconciled in the watcher

Mirror Codex's structure (placeholder bubble now, re-key the drained turn onto it later),
but **source the placeholder from the send-signal instead of a sidecar file.**

### Components
- **`AntigravityOutbox`** (new, per-agent) — holds messages sent-and-accepted-while-busy
  that have not yet appeared in the transcript. One entry: `{content, normalized_content,
  placeholder_event_id, sent_at}`.
- **`AntigravitySessionWatcher`** (existing) — gains outbox awareness: it emits the queued
  placeholder events and reconciles them against the transcript (the `_dedup_queued_turn`
  analogue).
- **`AgentManager.send_message_to_agent`** (existing) — after a successful send, tells the
  agent's watcher whether the send was queued.

### Data flow
1. **Send.** `send_message_to_agent(agent_id, content)` sends via mngr. On success it reads
   the agent's current `activity_state`; if it was **not IDLE** (busy → the message queued),
   it calls `watcher.note_queued_send(content)`. (If IDLE, the message will be processed
   immediately and show up in the transcript within ~1s — no placeholder needed, or emit a
   short-lived "sending" one; see §5.)
2. **Placeholder.** `note_queued_send(content)` synthesizes a `user_message` event with a
   **stable, content-derived id** (`f"agy-queued-{sha1(normalized_content)[:16]}"`), source
   `"antigravity/outbox"`, and pushes it through the same `on_events` path the transcript
   uses — so a queued bubble appears immediately. It records
   `normalized_content -> placeholder_event_id` in the outbox.
3. **Drain + reconcile.** When agy processes the message, the watcher's normal scan decodes
   it as a `USER_INPUT` step → a `user_message` event. Before emitting, the watcher runs the
   **reconcile step** (mirrors `codex._dedup_queued_turn`): if the event's normalized content
   matches an outbox entry, **re-key** the event's `event_id`/`message_uuid` to the
   placeholder's id and drop the outbox entry. Because the ids now match, the store
   *supersedes* the placeholder in place rather than appending a second bubble — the queued
   bubble becomes the real turn.

### Why this is clean
- The queued bubble and its drained turn share one id, so no double-render and no frontend
  reconciliation needed (unlike the `PendingMessages` optimistic path, which the product
  direction says not to depend on).
- Reconciliation lives in the watcher, next to the transcript, exactly as Codex does it.
- No screen-scraping; the only new coupling is one `send_message_to_agent` → watcher call.

---

## 3. Matching (the "fuzzy match" question)

Codex matches by **whitespace-normalized full content** (`normalize_user_content`), and flags
it as brittle (a mid-terminal edit diverges). For agy we can do **better than fuzzy**: the
parser already strips agy's `<USER_REQUEST>…</USER_REQUEST>` wrapper (§4 of the transcript
spec), so the drained `USER_INPUT` text equals the sent text. So an **exact
normalized-content match** is the primary key — reuse `normalize_user_content` verbatim.

Keep a **fuzzy fallback** only for the divergence cases (agy trims/reformats, or the user
edited the queued line in the terminal): match on a normalized **suffix/prefix of the last N
chars** (e.g. last 64) when no exact match is found. Guard it — only reconcile a fuzzy match
when exactly one outbox entry is a candidate, to avoid mis-pairing two similar messages.

---

## 4. Ordering + multiplicity
agy processes queued messages **FIFO, one turn each** (verified: multiple queued messages
stack in the pane and drain in order). The outbox is a list; each drained `USER_INPUT`
reconciles against the **oldest** matching entry, so two identical queued messages pair to
two turns in order. Event ids stay unique because a second identical message gets a
`-2` disambiguator suffix on collision (`agy-queued-{hash}` already present → append an
ordinal).

---

## 5. Edge cases
- **Sent while IDLE (not queued).** The message is processed right away and appears in the
  transcript within ~1s. Either emit no placeholder, or a short-lived `"sending"` variant that
  reconciles on arrival. Simplest v1: **no placeholder when IDLE** — the transcript event
  shows up promptly on its own.
- **Interrupt clears agy's queue.** agy drops queued messages on an interrupt. Wire the
  outbox to `AgentManager.reset_activity_state` (the interrupt path): **clear all
  placeholders** for that agent — they will never drain, so the bubbles must go. (This is the
  agy analogue of Codex's `turn_aborted` + the frontend's `clearQueuedMessagesOnIdle`.)
- **Never-drains safety net.** If the lifecycle returns to IDLE (turn + queue fully done) and
  an outbox entry still hasn't reconciled, drop it (it provably won't arrive) — mirror the
  frontend's working→IDLE queue-drain safeguard, but on the backend.
- **Restart.** The outbox is in-memory; a backend restart loses placeholders, but the drained
  messages still render from the transcript (just without the pre-drain "queued" affordance).
  Acceptable — no persistence needed.
- **Message typed directly in the agent terminal** (not via the UI). Signal A can't see it
  (no send went through the UI). It will still render correctly once drained (as a normal
  turn); it just won't show a *pre-drain* queued bubble. Signal B (§6) is the only way to
  catch these, at the cost of fragility.

---

## 6. Optional enrichment — pane cross-check (defer)
If we want queued bubbles for **terminal-typed** messages too, or want to detect when agy
**drops** a queued message before it drains, add a periodic pane read:
- Capture the pane; take the contiguous run of `▸`-prefixed lines immediately **above the
  input box separator** (this excludes the "Thought for Ns" `▸` line, which sits above the
  activity output, not adjacent to the input box).
- Each such line (de-wrapped) is a currently-queued message. Reconcile against / seed the
  outbox by normalized content.
- A queued message that **disappears** from that run without a matching transcript drain was
  dropped (interrupt/edit) → remove its placeholder.

This is authoritative to agy's real queue but brittle (wrapping, width, `▸` ambiguity,
scrollback, per-cycle tmux cost). Recommend shipping §2 first and adding this only if
terminal-typed queuing matters.

---

## 7. Build order
1. `AntigravityOutbox` (in-memory list + normalized-content index) + unit tests.
2. Watcher: `note_queued_send(content)` (emit placeholder) + reconcile-on-drain in the scan
   loop (re-key matching `USER_INPUT` events) + clear-on-reset.
3. `AgentManager.send_message_to_agent`: on success, read activity_state; if busy, call
   `note_queued_send`. Wire `reset_activity_state` → outbox clear.
4. Frontend: none required — queued bubbles arrive as normal `user_message` events (source
   `antigravity/outbox`) and supersede on drain via the shared event store. (A distinct
   "Queued" style pill can key off the `source`/a `queued: true` flag if desired.)
5. Manual tmux verification: busy agent, send 2-3 via the UI, confirm queued bubbles appear
   and each becomes the real turn as agy drains them; interrupt mid-queue and confirm the
   bubbles clear.

---

## 8. Limitations (explicit)
- Only **UI-sent** messages get a pre-drain queued bubble (Signal A); terminal-typed ones
  render only on drain, unless §6 is added.
- Reconciliation is content-based (exact-normalized primary, fuzzy fallback) — inherently
  imperfect if agy reformats the text or the user edits it in the terminal; the fuzzy guard
  limits mis-pairing but can't eliminate it. Same class of brittleness Codex accepts.
- In-memory only (no persistence across a backend restart).
