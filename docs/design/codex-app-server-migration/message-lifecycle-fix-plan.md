# Codex message-lifecycle fix plan (make codex satisfy the contract "to the T")

Status: FINAL — architecture settled; every claim confirmed by code inspection + live simulation
against a real `codex app-server` (codex-cli 0.147). This plan supersedes nothing in
`docs/design/harness-message-lifecycle-contract.md` (the contract); it is how codex comes to satisfy
every clause of it. Convention: **file reader** = the rollout-file watcher (`CodexSessionWatcher`,
tails codex's on-disk rollout JSONL); **ledger** = the subscribed `CodexMessageLedger` over the
app-server event stream (`CodexLiveConnection`).

---

## 0. The one root cause behind almost every symptom

**The production ledger connection is never subscribed to codex's event stream.** The app-server
streams a thread's `turn/*` and `item/*` notifications only to a connection that loaded the thread via
`thread/resume` or `thread/start`; a connection that binds via `bind_thread` (a local-only op, no RPC)
is **not** subscribed. `harnesses/codex/model.py::_bind_root_thread` (lines 117-136) calls
`bind_thread` whenever the root thread is loaded — which it always is post-create, because the agent's
`--remote` TUI keeps it loaded. So `CodexLiveConnection` runs blind: its only frame is
`thread/status/changed` (active→idle). Consequences, all live-verified:

- `_on_item_completed` (Delivered) never fires → a delivered message never leaves SENDING/QUEUED.
- `_on_turn_completed` → `_reconcile` never fires → the per-id delivered/returned settle never runs.
- The one event that does arrive, `idle`, triggers `_sweep_idle()`, which blindly flips every live
  entry to RETURNED.

Every field symptom is a face of this one gap:

| Symptom | How the subscription gap produces it |
|---|---|
| Duplicated message + stale chip (Bug 1) | chip never removed on delivery (no `item/completed`), so it coexists with the file reader's transcript turn for the whole turn |
| Shoulder-tap "in flight" 500 (Bug 2) | opening send stays SENDING the whole turn, so `is_sending()` is true → the endpoint 500s |
| Stop returns everything (Bug 3) | nothing reaches DELIVERED, so at idle `_sweep_idle` returns every message; the next stop hands them all to the composer |

**Fix 0 (the linchpin): make the ledger connection subscribe** — load the root thread via
`thread/resume` for the live-connection path instead of `bind_thread`. Leave the model-switch path
(`open_bound_codex_client` used by `CodexModelResolver`) on `bind_thread`; it only needs a bound thread
for a settings write. Give the live connection its own `open_subscribed_codex_client` (or a
`subscribe: bool` seam) that resumes.

**Live-verified safe (Q3):** `thread/resume` triggers NO history-notification replay (history is
pulled via `thread/read`, not pushed — `initialTurnsPage` is null), does NOT perturb the daemon or the
concurrent `--remote` TUI, and subscriptions are ADDITIVE (a later TUI/model-switch resume never
steals the ledger's stream — verified with A+B concurrent subscribers). On connect the ledger seeds
itself: the resume response's `status` + one `thread/read{includeTurns:true}` give the in-progress turn
id and already-committed items, so a mid-turn reconnect starts correct; live events flow from there.

---

## 1. The architecture — who owns what

One division of labor, and it is the structural fix for the two-unordered-channels double-show:

> **Everything already in the chat → the file reader. The live edges → the ledger.**

- **File reader** owns the **committed transcript**: all history and every committed turn — user
  turns, agent messages, tool calls, reasoning — read from the on-disk rollout. Reading the file gives
  history for free (no separate fetch), and the file is the durable record a reload rebuilds from.
- **Ledger** owns the **live edges** — the not-yet-committed states: the **queue** (chips) and
  **in-flight Sending**, keyed by codex's own `item.id`.

The two never contend, because **the only thing that is ever both a chip and a committed turn is a
user message.** Agent output (replies, tools, reasoning) is never queued, so the file reader owns all
of it with zero collision. That leaves exactly **one** boundary in the whole system to order: a user
message crossing queued/sending → committed.

**The single ordered handoff (this is the A3b fix):**
- User-turns are emitted by the **file reader on initial hydration only** — a page load / reload
  rebuilds the whole committed transcript from disk, user turns included.
- During **live** operation, every user-turn is emitted by the **ledger** at the moment it commits: the
  ledger removes the chip, then emits the user-turn — in that order, one step, no race. The file reader
  **suppresses live user-turns** (it emits agent output live, user-turns only on hydration).
- The two agree on codex's `item.id` (written in the rollout file too), so the file reader knows which
  user-turn the ledger already owns and does not double-emit it.
- `thread/read` fetches the authoritative committed form of the crossing message at the boundary, and
  is the backstop when the commit event is ambiguous.
- This holds for foreign (TUI-typed) user messages too: the subscribed ledger sees their
  `item/completed` and emits the live user-turn source-agnostically; the file reader still suppresses
  live user-turns. So there is one rule, no special-casing: **live user-turns come from the ledger;
  everything else (agent output live, and the full transcript on hydration) comes from the file
  reader.**

This is claude's single-authority guarantee applied exactly where it is needed — one owner for the one
message type that can double-show, with a strictly ordered chip-out-then-turn-in handoff — while
keeping the file reader for agent output and history, which it already produces, and gets for free,
from disk.

### claude ↔ codex parity

| Concern | claude (reference) | codex (this plan) |
|---|---|---|
| Committed transcript + history | `ClaudeSessionWatcher` over the session file | file reader over the rollout file |
| Queue + in-flight (the live edges) | `SendingRegistry` + `ClaudeQueueTracker` on the watcher | the subscribed ledger, keyed by `item.id` |
| Delivery = commit | LEAVE + `user` record in the file | `item/completed(userMessage)` / `thread/read`, by `item.id` |
| Message identity | session-derived / positional; frontend id is a throwaway Sending token | codex's `item.id`; **we mint nothing** |
| A3b ordering | one file pass emits chip-removal then turn | the ledger emits chip-removal then the live user-turn |
| Availability | backend `shoulder_tap_available` flag | same flag, from `ledger.is_sending()` |
| Source-agnostic | reflects every enqueue, TUI-typed or sent | reflects every committed message by `item.id` |
| Frontend | paints Sending, drops positionally by backend id | identical (unchanged) |

---

## 2. The fixes

### Fix 0 — Subscribe the live connection (linchpin)
As §0. Prerequisite for everything else.

### Fix 1 — The committed / not-committed split + the ordered handoff (A3b, A1)
Implement §1. Concretely:
- The ledger, subscribed, tracks the queue + Sending by `item.id`. On a `userMessage`'s
  `item/completed`, it removes the chip and emits the live user-turn (chip-removal broadcast first,
  then the transcript-turn event), then reconciles per `item.id`.
- The file reader: on hydration (page load / new watcher), it emits the full committed transcript from
  the file, user turns included; during live tailing it emits agent output (assistant / tool /
  reasoning) but **suppresses user-turns** (the ledger owns those live). Both key user-turns on
  `item.id` so hydration vs live can't double-emit the same message.
- `thread/read` at the boundary supplies the authoritative committed user message and backstops an
  ambiguous commit event.

### Fix 2 — Key on `item.id`; mint nothing; source-agnostic (A3, A4)
- Track every `userMessage` from the subscribed stream by its app-server `item.id`, regardless of
  origin. Stop threading a minted `clientUserMessageId` as the delivery key; reconcile on `item.id`.
- The web-UI "Sending…" bubble drops positionally by backend `item.id` (claude-identical); no minted
  correlation id anywhere.
- **Confirmed hard app-server limit (Q4):** pending messages are invisible to every server-side read
  until they commit, and the `--remote` TUI holds a queued message client-side until it submits it. So
  a message another client has typed but not yet submitted is not on the server at all — nothing to
  mirror, like another client's unsent draft. It surfaces the instant codex commits it (via `item/*`,
  keyed by `item.id`). Accepted; no polling would help.

### Fix 3 — Shoulder-tap = interrupt to send the queue in EARLY (Shoulder-tap)
A shoulder-tap prematurely delivers the queue by interrupting the current turn — it does NOT wait for
codex's next natural yield. **Confirmed (Q5): interrupt+resend is codex's own native mechanism.** The
136-method RPC surface has no flush/yield/commit-pending; the real `--remote` TUI HOLDS a queued
message client-side and its "send early" (Esc) is, frame-for-frame, `turn/interrupt` (terminal) then a
fresh `turn/start` carrying the held text. So our backend doing interrupt+resend is identical to the
codex TUI (the native reference; it differs from claude only because claude's harness auto-flushes and
codex exposes no such call).

The backend tap:
1. Capture the queued messages (send order) from the ledger.
2. `turn/interrupt` the current turn.
3. Reconcile per `item.id`: anything committed before the interrupt stays Delivered (not resent); the
   rest are what to send.
4. `turn/start` ONE fresh turn with those messages concatenated in send order (combining required —
   individually the first opens a turn and the rest re-queue). Delivered immediately.

On success the queued entries resolve to **Delivered** (their text committed via the combined turn),
not Returned; the ledger removes their chips before emitting that turn (A3b), and the block is one
turn, so it is atomic — never half-delivered. As with claude's tap, this ends the current turn (its
partial output commits as interrupted) rather than injecting into a still-running turn: codex exposes
no early-flush, so end-and-resend IS the "deliver now" mechanism (Part C — differ in mechanism,
identical in behavior).

**Continuously visible through the flush (A1a) — the messages must NOT blink out.** The tap is its own
ledger path (`shoulder_tap()`), distinct from stop's `interrupt()`: stop *returns* the queued messages
to the composer; the tap *delivers* them. So they must stay on screen the whole time. The ledger keeps
each queued entry visible across the interrupt and the resend — it is never momentarily Returned to
the composer and never dropped — and flips it to a committed turn only when the combined `turn/start`
commits (chip out, then turn, per A3b). **Decided: rendering (b).** On tap, the ledger
transitions the queued entries to a backend-reported **Sending** state, so they read "Sending…"
through the resend (mimicking a fresh send), then flip to the committed turn when the combined
`turn/start` lands. This needs one small addition — an in-flight state on the wire: today "Sending…"
is painted only by the frontend on its own POST, so we add a backend-reported Sending state (the
resend is backend-initiated, so the backend must report it; the frontend just renders what it is
told, per A2). What we will NOT do is remove the chip before the resend lands — that is the blink-out
to avoid.

**Endpoint.** The tap is the native `POST /shoulder-tap-atomic` (the button the frontend already
wires; `Response.ts` `shoulderTap`), greyed by `shoulder_tap_available`. Its codex branch — today a
no-op gate check — becomes the interrupt+resend above. The separate `POST /flush-queue`
(`_flush_queue_endpoint`) is the legacy restart-drain (SIGKILL-restart + resend) for harnesses with no
native interrupt; **codex does not use it** (codex interrupts natively via the ledger). The stop
button is `POST /drain-to-composer` (Fix 4). So: tap = shoulder-tap-atomic → deliver; stop =
drain-to-composer → return.

- **Availability = `not ledger.is_sending() and queue_non_empty`**, surfaced as the backend
  `shoulder_tap_available` flag so the button GREYS while anything is Sending (today it never greys
  because the codex send path bypasses `mark_send_in_flight` and the flag never consults
  `is_sending()` — Bug 2's real cause). A tap that still races a send is a **benign 200 no-op**, never
  a 500 dialog (match pi's `SEND_IN_FLIGHT`).
- **Reliable:** `turn/start` reliably opens a turn (live-verified), so the resent block always lands.
- **No new strand-backstop needed (Q1):** an accepted steer against a live turn is always delivered at
  the yield boundary (22/22); the only non-delivery is a `turn/steer` rejected `-32600 "no active turn
  to steer"` (the ~8 ms window where `turn/start`'s response precedes steerability, or an already-ended
  turn), which `submit()` already handles by clearing the active turn and re-submitting as a fresh
  `turn/start` (its existing ABA retry).

### Fix 4 — Interrupt is terminal; return only not-Delivered (Interrupt, A4, A5)
Live-verified: `turn/interrupt` is terminal — an accepted-but-uncommitted steer is dropped; the turn
ends (`turn/completed(interrupted)`, idle, `active_turn_id` cleared). Claude's "interrupt, verify
still-thinking, send Proceed" is impossible for codex (nothing thinks after; a follow-up steer errors
`-32600`; the lost steer is gone). Strategy: terminal interrupt.
- Stop → `turn/interrupt`; after the abort settles, reconcile per `item.id`: committed → Delivered
  (stays), uncommitted → Returned (to composer, in send order, prepended). Delivered never returns.
- **Returns queued AND in-flight Sending, in send order (the interrupt-during-flush case).** Every
  message is a ledger entry carrying a send-order number (`send_seq`) from the instant it is accepted
  as Sending — not only once it is Queued. So the return block is *every* non-committed live entry,
  Queued and in-flight Sending alike, sorted by `send_seq`, prepended on top of the composer's current
  text. This matches claude/pi (their queued block + in-flight block, in send order). Concretely this
  fixes a gap in today's `interrupt()`: it reconciles only entries bound to the active turn, so a
  Sending entry still mid-`submit` (no disposition yet, so unbound) is missed — the new interrupt
  reconciles ALL live entries by `send_seq`, bound or not.
- **The in-flight-send race guard.** A Sending message can be mid-`submit` when Stop fires. The
  client's frame lock serializes a send against the interrupt, so the interrupt sees a settled
  disposition for any send whose `submit` already returned. For a send whose `submit` result lands
  *after* the interrupt cleared the turn, a generation counter (bumped on every interrupt) makes that
  late send reconcile itself as Returned instead of silently opening a fresh turn on the now-idle
  daemon — the one trap in this corner, explicitly closed.
- **Async (A5):** the interrupt RPC blocks until the turn actually aborts (up to ~4.4 s mid-stream).
  Clear the dot and return optimistically; do not block the user on the RPC.
- Fix `_sweep_idle` to reconcile via `thread/read` rather than blind-return (defense in depth; with
  subscription, `turn/completed` reconciles before idle anyway). Keep the once-only
  `_take_returned_block`; never use the cumulative `reconcile_returned()` for the composer.

### Fix 5 — Activity dot: nothing to do (A6)
The dot already works (transcript turn-latch) and is KEPT as-is, permanently. A ledger-sourced dot is
explicitly NOT wanted and will never be built — do not wire the ledger's `turn/*` events to the dot.
A6 is already satisfied by the transcript latch.

### Fix 6 — The conservation test drives BOTH channels (Part D) — the guarantee
Port claude's `test_claude_message_lifecycle_conservation.py` to codex: drive the ledger's queue
snapshot AND the file reader's transcript emission together, in randomized Send/queue/tap/interrupt
interleavings, asserting after each step: exactly-one-state conservation, order preserved, returns
prepended in send order, and **A3b ordering across the two channels** (chip-removal before the
transcript turn), with interrupt-during-flush required — specifically a stop while the queue is
non-empty AND a message is in-flight Sending, asserting both return together in send order on top of
the composer text. Codex's current test drives only the ledger,
so the cross-channel double-show is invisible to CI — that is why the suite is green while the product
double-shows. This test is what makes the plan enforceable.

---

## 3. Contract conformance — every clause settled to the T

| Clause | Requirement | How this plan satisfies it |
|---|---|---|
| **A1** five states + conservation | each message in exactly one of Composer/Sending/Queued/Delivered/Returned; `delivered+queued+sending+returned = total` | Ledger holds Sending/Queued/Returned by `item.id`; Delivered = the committed transcript. Every accepted message is in exactly one; Fix 6 asserts conservation after every step. |
| **A1a** never swallowed | continuously visible, live, no reload | Fix 0 makes delivery recognized promptly (no more sweep-to-Returned that vanished delivered messages). Sending bubble → chip (ledger) or committed turn — real rep appears first, then Sending drops. No gap, no reload dependence. Returned only arises where a hand-off exists — a failed send restores the text on the POST response; interrupt returns the block — so no Returned message is orphaned off-screen. |
| **A2** dumb frontend, backend optimism | frontend paints only "Sending…", never self-resolves; all state/availability backend | Unchanged frontend paints Sending and drops it positionally by backend `item.id`; queue, delivery, availability all come from the backend (ledger + file reader). |
| **A3** queue fidelity | UI queue = the harness's real queue | Queue = ledger's pending steers, keyed by `item.id`, source-agnostic, **minting nothing**. Honest limit (Q4): a not-yet-submitted message from another client isn't on the server; it appears at commit. |
| **A3b** ordered transitions | chip removed before the turn appears; never two states at once | The one collidable message (a user message) is owned by the ledger at commit: chip-removal emitted before the user-turn; file reader suppresses live user-turns (Fix 1). Fix 6 asserts the ordering across channels. |
| **A4** delivery = COMMIT, per id | Delivered only when committed; reconcile per stable id | Delivered = committed `userMessage` observed by `item.id` (`item/completed` / `thread/read`), never the ack. Interrupt/settle reconcile per `item.id`. |
| **A5** fast | send/queue/tap/interrupt prompt; interrupt immediate | Subscribe = immediate events (no poll). Interrupt returns/clears the dot optimistically instead of blocking on the multi-second RPC (Fix 4). |
| **A6** activity exact | dot reflects real generating state; clears immediately; no Sending/Thinking overlap | Transcript turn-latch (already exact and kept, permanently — no ledger-sourced dot, ever). Sending drops as the committed turn appears, so no overlap. |
| **Send** | Sending → Delivered/Queued/Returned; real rep first | `ledger.send` → STARTED (Sending) / STEERED (Queued) / error (Returned); the chip or committed turn appears before Sending drops. |
| **Queue** ephemeral | survives a UI reload (session alive); dies with the session; never revived/auto-sent | The ledger is server-side (`CodexLiveConnection`, tied to the daemon session, not the UI), so a UI reload re-fetches the same live queue — it persists. But it dies with the session: a new session builds an empty ledger and nothing from a dead session is revived (Fix 0's resume seeds live state, not a queue). |
| **Shoulder-tap** | deliver all parked messages now, in order; unavailable while Sending | Interrupt + resend the queue as one `turn/start` (Fix 3), the TUI-native early send; availability = `not is_sending() and queued`; racing tap = benign 200. |
| **Interrupt** | stop; return every not-Delivered in send order, prepended; Delivered stays | Terminal `turn/interrupt` + reconcile per `item.id`; every not-committed live entry — Queued AND in-flight Sending — returns by `send_seq`, prepended; committed stay Delivered; async so Stop is immediate (Fix 4). |
| **Part D** | one conservation test per harness, interrupt-during-flush required | Fix 6: dual-channel conservation test with the required interrupt-during-flush case. Green ⇒ conforms. |

---

## 4. Live-confirmed facts (all resolved against a real daemon)
- **Q1** No natural strand — accepted steers always deliver at the yield boundary (22/22); the only
  non-delivery is the `-32600` reject race, already handled by `submit()`'s ABA retry.
- **Q2** submit-while-idle cleanly starts a new in-order turn; a steer at a dead turn errors `-32600`,
  so a returned/stranded message is resent as `turn/start`, not `turn/steer`.
- **Q3** `thread/resume` is safe: no history replay, no daemon/TUI perturbation, additive
  subscriptions; seed on connect via resume-status + one `thread/read`.
- **Q4** pending steers (own or foreign) are invisible to server-side reads until commit → foreign
  messages surface at commit via `item/*`.
- **Q5** no native early-flush anywhere in the 136-method surface; the TUI's own "send early" is
  interrupt+resend.

## 5. Implementation sequencing
1. **Fix 0** (subscribe) — smallest change; restores commit-based delivery. Most bugs die here.
2. **Fix 2** (key on `item.id`, drop minting, source-agnostic) + **Fix 1** (the split + ordered
   handoff: ledger emits live user-turns, file reader suppresses them).
3. **Fix 3** (shoulder-tap: availability flag + interrupt-and-resend) + **Fix 4** (terminal interrupt,
   async, `_sweep_idle` reconcile).
4. **Fix 6** (dual-channel conservation test) — gates "done."
5. **Fix 5** — nothing to do; the transcript-latch dot stays (no ledger-sourced dot, ever).
