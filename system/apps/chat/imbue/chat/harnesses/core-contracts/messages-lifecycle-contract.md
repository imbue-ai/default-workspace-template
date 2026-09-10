# The message-lifecycle contract (CANONICAL)

The single source of truth for what happens to a user's message across every harness
(claude, codex, pi, antigravity): send, queue, shoulder-tap, interrupt, and return-to-composer. Harness
implementations differ; **the observable behavior is identical**. If an implementation
disagrees with this doc, the implementation is wrong.

`messages-lifecycle-contract-state-of-things.md` records where reality falls short of it: the
per-harness conformance gaps, the upstream
behavior we do not control, and what we chose not to build.

---

This file is the TIMELESS half: the invariants and per-operation contracts every
harness must satisfy. It says nothing about how any particular harness satisfies them,
and nothing about what is currently broken -- both of those live in
`messages-lifecycle-contract-state-of-things.md` beside it, so this file only changes
when the CONTRACT changes.

---

## Part A — Core invariants (these shape everything)

### A1. Message states — every message is always in exactly ONE
| state | meaning | UI |
|---|---|---|
| **Composer** | draft text, not sent | text in the input box |
| **Sending** | submitted, in flight, not yet confirmed | "Sending…" |
| **Queued** | accepted, parked in the harness's own queue (agent is mid-turn) | a queued chip |
| **Delivered** | committed as a user turn in the agent's durable conversation | a user turn in the transcript |
| **Returned** | came back to the composer, never delivered | text back in the input box |

**Conservation:** a message is always in exactly one state; every transition is explicit and
observable. No message ever vanishes (no state) or ghosts (shown in a state it is not in).
At any instant: `delivered + queued + sending + returned = total accepted`.

**A1a. NEVER SWALLOWED — continuously visible, live, without a reload.** From the instant a
message leaves the composer it is **continuously visible on the user's screen** — as "Sending…",
a queued chip, or a transcript turn — until it is Delivered or Returned. It never disappears
(not even momentarily, beyond the ordered handoff windows of A2/A3b), never "resurfaces later,"
and **never requires a page reload to appear**: the live stream alone always reflects its true
state. **Any message that has left the composer and has not returned to it is ALWAYS somewhere
on the user's screen.** The forbidden failure — "swallowed" — is a message that vanishes after
send and only shows up in the queue or transcript later, or only after a reload. The backend
must stream the message's real state to the live view *immediately*, never leaning on a later
poll, a discovery cycle, a reconnect, or a reload to make it reappear. If the backend's own
knowledge lags (e.g. a not-yet-parked send), the message stays visible as "Sending…" until the
real state arrives — it is never dropped from the screen in the meantime.

### A2. THE FRONTEND IS DUMB — backend owns all state and every decision; only "Sending…" is optimistic
The frontend renders exactly what the backend reports and captures input. It **never makes a
lifecycle decision on its own** and computes nothing about conservation, ordering, delivery,
return, or availability — all of that is backend-side. It does not move a chip, mark a message
delivered, change the model chip, or clear the queue ahead of the backend confirming it.
- The queue chips = whatever the backend says is queued. The model/effort/fast chip = whatever
  the backend reports as the live value; it does **not** move on click — it moves when the
  backend confirms the change, and shows a popup when the backend reports the change failed.
- **Shoulder-tap:** the frontend does not track the queue — it asks the backend "what is queued,
  what is still Sending, and is the tap available?" and renders/acts on the answer.
- **Interrupt (incl. mid-shoulder-tap):** the frontend does not decide what returns — it asks
  the backend "what exact text do I place in the composer?" and places it.

**The ONE permitted optimism — "Sending…", and it is BACKEND-DRIVEN.** The single optimistic
state the frontend may invent is **"Sending…"**, shown immediately after it POSTs a message to
the backend, when it does not yet know anything else about that message's state. Nothing else is
ever optimistic.

The principle: **backend-driven optimism.** The frontend may *paint* the "Sending…" placeholder,
but it never *resolves* it — every transition out of "Sending…" (removal, correlation to a chip
or turn) is driven by the backend reporting the real state. The frontend never decides on its
own when "Sending…" ends: no self-timer, no positional guessing, no anti-strand fallback. This
is the difference between a placeholder that reliably hands off to reality (backend-driven) and
one that *swallows* the message (frontend-driven optimism resolving itself — e.g. a 6-second
timer that removes the bubble before the backend has confirmed anything). Optimism you may show;
optimism you may not self-resolve.

**Reconciliation goes THROUGH the message (no gap).** "Sending…" is removed only once the
message's **real** representation appears — its queued chip or its committed transcript turn
(the queue is part of the transcript, so either counts). The real representation appears
**first**, and only then is "Sending…" removed — so from the POST onward the message is *always*
visible in some form; there is never an instant where it is in no visible state. A brief overlap
(real + "Sending…") is fine; a gap is not. The backend drives the removal by reporting the real
state; the frontend removes "Sending…" on that report, not before.

Beyond "Sending…", the only other allowance is briefly HOLDING the last state the backend
reported during a round-trip (lag, not invention) until the next update arrives.

**Optimism is a symptom, not a feature.** A placeholder exists only because the send beneath it
is not robust: if delivery were certain and immediate there would be nothing to paint over. So
the amount of optimism a harness needs is a direct measure of how unreliable its send path is,
and the way to reduce it is to make sending robust -- not to make the placeholder cleverer.
Treat every optimistic state as debt owed by the layer below it.

That is also why the frontend is forbidden from resolving it. A self-timer would hide the debt
rather than pay it: the message stops being visible while the underlying send is still just as
unreliable, which trades a confusing UI for a lost message. See E16 in the state-of-things file for what this costs when
the send path does fail catastrophically.

Keep both sides as simple as possible; push every decision down.

### A3. Queue fidelity — the UI queue IS the harness's own queue
The set of Queued messages equals the harness's real internal queue exactly (codex: its
pending steers; claude: its parked send queue; pi: its inbox) — no parallel UI queue, no
drift, no phantom chips, no missing chips. A chip exists iff the harness has that message
parked.

### A3b. Queue transitions are reflected — "message in, message out", ORDERED through the message
Fidelity is dynamic, not just steady-state: every add and every removal is reflected promptly,
and transitions are **ordered** so the message is never shown in two states at once and never
disappears into a gap.
- A message that **enters** the queue appears as a chip.
- A message that **leaves** the queue — because it was Delivered (committed as a turn) or
  Returned (interrupt) — has its chip **removed** and appears in its new state (a transcript
  turn, or text back in the composer).
- A message is **never shown in two states at once** (e.g. a queued chip AND a delivered turn),
  and a message that has left the queue is **never** left showing a stale chip.

**Ordering — the transition goes THROUGH the departing representation.** For a message leaving
the queue into the transcript (Queued→Delivered), the **queue update (chip removal) is emitted
first, then the transcript turn appears** — never the transcript turn before the chip is gone.
So a message that has left the queue does not show in the transcript while still a chip (no
double-show). (Mechanically: emit the queue-removal update before — or atomically with — the
transcript event, never after; this is the fix for the two-unordered-channels double-show.)

This is the general rule for transitions between two **real** states: remove the old before
showing the new (depart-before-arrive), so there are never two real states at once. The one
inversion is the fake **"Sending…"** placeholder (A2): because it is optimistic, not a real
state, its removal is deferred until the real state has appeared (arrive-before-depart), so
there is no gap. Real→real: old goes first. Placeholder→real: real goes first.

So: message in → chip appears; message out → chip disappears (first) and then it shows where it
went. The queue view is a live mirror of the harness queue's membership, add and remove alike.

### A4. Delivery bar = COMMIT, not ack
A message is Delivered when, and only when, it is committed as a user turn in the agent's
durable conversation (transcript / rollout / session). Not when typed, not when a send/steer
call returned "accepted," not when optimistically shown — **committed**. Every message carries
a **stable id the backend mints at send time**; delivery is decided by checking that id
against the committed transcript, never guessed. An "accepted" ack is not proof of delivery
(a message can be accepted into a turn that is then interrupted before it commits — verified
live on codex). After every turn-settle and every interrupt the backend reconciles per id:
committed → Delivered; not committed → Returned.

### A5. Everything is fast
Send, queue, shoulder-tap, interrupt, and return-to-composer all complete in a short, bounded
time. Interrupt in particular must feel immediate — the turn stops and the activity indicator
clears promptly, not after a long wait.

### A6. Activity indicator is EXACT to the harness — not a second over or under
The activity dot ("Thinking…" / tool-running / idle) reflects the harness's **real** generating
state, read directly from the harness's turn state (the transcript / turn events), never
computed or timed by the frontend.
- **Model generating → dot shown. Model done → dot cleared IMMEDIATELY** on the turn-completion
  signal — not after a poll, a settle timer, or a staleness window. It must not linger.
- **RUNNING lasts until the turn is FULLY done — not when token-generation stops.** "Done" is the
  turn-completion signal (codex: `turn/completed`), i.e. the moment the finished message actually
  lands in the web chat — NOT the earlier instant the model stops producing tokens while it is
  still flushing/committing its output. Keep the dot up through that tail: RUNNING until the very
  last millisecond the message appears on screen, then clear. No more (no linger past it), no less
  (never go idle while output is still being committed). This is exactly the old codex bug to
  avoid — the state going idle while the model was still spitting its answer out to the terminal.
- **Inherent transport latency is fine** (the time to read the transcript/event); **artificial
  lag is a violation** — no fixed post-turn delay, no "~2s after the model finishes the dot is
  still there," no polling cadence that rounds the state.
- **No overlap between "Sending…" and "Thinking…".** "Sending…" must be gone before/as the turn
  starts generating — the model beginning a turn is exactly the point the message committed
  (its real state appeared), which per A2 is when "Sending…" is removed. "Thinking…" starting
  while "Sending…" is still shown is a violation (the two states co-existing on one message).
- On interrupt, the backend emits the state change so the dot clears immediately and the
  transcript shows the correct interruption marker (`[Request interrupted by user]`,
  `[Request interrupted for tool call]`, etc.).

**Known violations to eliminate (codex, today):** (1) "Thinking…" begins before "Sending…"
disappears (send/commit ordering not honored → overlap); (2) the dot lingers ~2s after the
model finishes (rollout-poll + staleness guard instead of a direct turn-completed signal).
Both are artificial lag/ordering bugs, not inherent latency — unacceptable. The app-server's
`turn/started` / `turn/completed` events (immediate, no poll) and its synchronous send ack are
the mechanism that makes codex exact here; the codex plan must adopt them for activity, not the
rollout-poll.

---

---

## Part B — Per-operation contracts

### Send
On POST the frontend shows "Sending…" immediately (the one permitted optimism, A2). A
successful POST means the harness ACCEPTED the message; within a bounded time it then resolves
to exactly one of Delivered (agent was idle: the message starts and commits a turn) / Queued
(agent busy: parked in the harness queue) / Returned (accepted but never committed — the
Interrupt/Queue rules). On resolve, the real representation (transcript turn or queued chip)
appears first and only then is "Sending…" removed — reconciliation goes through the message, so
it is never in a no-visible-state gap. A message the harness never accepted must resolve as a
FAILED POST — the error response is what restores the text to the composer — never as a
quietly-recorded Returned, which no surface shows and which would leave "Sending…" stuck
waiting on an arrival that cannot come. Never stuck Sending, never silently gone.

### Queue — an EPHEMERAL STORE, bound to the session's life
A Queued message persists across UI reloads (the *session* is still alive, so its queue is still
real), preserves order, mirrors the harness's own queue (A3), and is eventually either Delivered
(flushed, or consumed at the harness's delivery point) or Returned (interrupt). Never silently
dropped while the session lives.

**The queue LIVES AND DIES WITH THE SESSION — it is an EPHEMERAL STORE.** It is NEVER persisted
across a session/agent restart, NEVER revived, and NEVER auto-sent on resume. When the agent is
stopped (or destroyed, or a new session begins) the queue is *gone*: a fresh session starts with
an empty queue and nothing from the old one is replayed, rebuilt, or delivered for the user.
Surviving a UI reload (the session is still alive) and surviving a session restart (the session
died) are DIFFERENT things — only the former holds. There is no durable queue file that outlives
the session and no mechanism that ever auto-flushes the queue on the user's behalf.

**Consequence (falls out):** the queue is empty whenever the agent is stopped and whenever a
session is new. There is no such thing as a queued message with no live session to eventually
consume it.

### Shoulder-tap (flush)
"Deliver all parked messages to the agent now." Injects the Queued messages into the running
turn, in order; each independently resolves to Delivered (committed) or, if not committed,
stays Queued / becomes Returned (per Interrupt). Never half-delivers a message.
**Availability:** unavailable while ANY message is Sending (a Sending message has not resolved,
so flushing "the queue" would miss or race it). Available iff (nothing is Sending) AND (the
harness queue is non-empty). The frontend learns availability + contents by asking the backend.

### Interrupt (stop) — the load-bearing one
Interrupt does two things, atomically from the user's view:
1. **Stops the agent's current turn** (activity indicator clears per A6; transcript shows the
   interruption marker).
2. **Returns to the composer every message that is NOT Delivered** — every Queued message and
   every in-flight Sending message that has not committed. Delivered messages stay Delivered
   (their turn may render interrupted/partial; the message itself remains).

**Return ordering and placement:** returned messages are placed into the composer **in the same
order they were sent**, and **on top of** (prepended to) whatever text is already in the
composer at that moment. The backend computes the exact ordered text to return; the frontend
prepends it.

**Interrupt during a shoulder-tap** is just this contract applied: the tap is an attempt to
deliver all queued messages; if interrupt fires during it, each queued/in-flight message that
did not commit returns to the composer (in send order, on top), and each that did commit stays
Delivered. The backend decides per id (A4); the frontend asks the backend what to place.

---

---

## Part D — Enforcement

One conservation test per harness: drive Send / queue / shoulder-tap / interrupt in randomized
interleavings and assert after each step that every message is in exactly one state and
`delivered + queued + sending + returned = total sent`, zero lost, zero ghosts, order
preserved, returns prepended in send order, and that every queue add/removal is reflected with
nothing double-shown or left stale (A3b). **Interrupt-during-flush is a required case.** A
harness conforms to this contract only when its test is green.

---
