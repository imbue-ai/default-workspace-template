# The message-lifecycle contract (CANONICAL)

The single source of truth for what happens to a user's message across every harness
(claude, codex, pi, antigravity): send, queue, shoulder-tap, interrupt, and return-to-composer. Harness
implementations differ; **the observable behavior is identical**. If an implementation
disagrees with this doc, the implementation is wrong.

Part E records where reality falls short of it: the per-harness conformance gaps, the upstream
behavior we do not control, and what we chose not to build.

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
unreliable, which trades a confusing UI for a lost message. See E16 for what this costs when
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

## Part B — Per-operation contracts

### Send
On POST the frontend shows "Sending…" immediately (the one permitted optimism, A2). Within a
bounded time the backend resolves the message to exactly one of Delivered (agent was idle: the
message starts and commits a turn) / Queued (agent busy: parked in the harness queue) / Returned
(send failed). On resolve, the real representation (transcript turn or queued chip) appears
first and only then is "Sending…" removed — reconciliation goes through the message, so it is
never in a no-visible-state gap. Never stuck Sending, never silently gone.

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

## Part C — Harness implementations (differ in mechanism, identical in behavior)

- **claude (today, heuristic — works but is the messy path):**
  - *Shoulder-tap:* press the chord keybind to interrupt, check whether it is still running,
    then send the new message afterward. Heuristic, but functional.
  - *UI interrupt:* the backend checks the queue; if empty → the same chord interrupt; if
    non-empty → determine the queued messages from the backend, return them to the composer (in
    order, on top), then a full restart.
  - *Known gap:* claude does not yet reliably honor Interrupt §return (in-flight/queued messages
    are not always returned to the composer). This is the bug to fix so claude matches this
    contract.
- **codex + pi (in-house — the clean path):** shoulder-tap and interrupt are native (control
  line / sentinel today; JSON-RPC for codex next), so they are faster and always go down the
  same single code path — no heuristic branch. codex on the app-server is the cleanest: send =
  `turn/start`/`turn/steer`, stop = `turn/interrupt`, queue = pending steers observed via
  events, delivered = the committed `userMessage`, reconciliation by the minted `clientId`.

- **antigravity (the queue is OURS, and one typist owns it):** agy is the only harness whose
  queue cannot be observed at all -- it parks mid-turn input inside its TUI, invisible on disk,
  and merges everything parked into one turn. So it is never allowed to park anything. `send`
  does not type: it enqueues, publishes the chip inside the POST, and wakes the ONE typist, a
  per-agent worker that delivers the whole block only when its bounded predicate says no turn
  is open. queue = ours, journalled per-session so it survives a backend restart but never the
  agy session. delivered = agy's own store gaining a user turn whose text is our block, NOT
  mngr's ack (whose only probe is a busy-marker mtime that a parked message also advances).
  stop = one ctrl+c plus handing back what we still hold, taking care not to take entries a
  flush is mid-send with. tap = one ctrl+c, then wake the worker -- the tap never sends,
  because a second typist could deliver the same block twice. It needs no restart to stop
  (nothing is parked inside agy to retract), keeping the restart only as the hammer for a
  cancel that cannot be trusted to have landed.

The target is to make them converge on the clean, single-path shape; claude's heuristic
path is what most needs to move toward it.

---

## Part D — Enforcement

One conservation test per harness: drive Send / queue / shoulder-tap / interrupt in randomized
interleavings and assert after each step that every message is in exactly one state and
`delivered + queued + sending + returned = total sent`, zero lost, zero ghosts, order
preserved, returns prepended in send order, and that every queue add/removal is reflected with
nothing double-shown or left stale (A3b). **Interrupt-during-flush is a required case.** A
harness conforms to this contract only when its test is green.

---

## Part E — Known limitations

Where the implementations do not fully meet Parts A–D, and why. E1–E3 are conformance gaps
against the contract itself; E4–E8 are upstream behavior we do not control; E9–E10 are the test
quirks and what we chose not to build.

### E1. claude and pi — queued chips blink out during a restart-based shoulder-tap (A1a)
**A visual blip, not a lost message.** The shoulder-tap never returns anything to the composer —
it drains the queue and **resends it**. On claude and pi that goes through `restart_drain`: the
queue block is captured, the agent is restarted, and the block is resent as one merged turn.

The restart clears the harness's own queue, so the chips disappear at that instant — but the
resent block is not a frontend POST, so no "Sending…" bubble covers the window. The messages are
briefly in no visible state, then reappear a moment later as a committed turn. They are never
lost; A1a's "not even momentarily" is what this misses.

**codex does not have this.** Its atomic shoulder-tap merges into the live turn with no restart,
and the ledger marks each chip `is_sending=True` while it re-sends, so the chip stays
continuously visible and renders as "Sending…" instead of blinking out. Closing the gap on
claude/pi means the same thing: keep the captured block visible as sending chips across the
restart window rather than letting the queue snapshot go empty.

### E2. claude — a send holding `message.lock` past the bounded wait (stop, not shoulder-tap)
**Rare, and stop wins by design.** This is the **stop button**, not the shoulder-tap. Stop takes
mngr's per-agent `message.lock` with a bounded wait, then refreshes the mirror and captures the
block under it — so a message that parked between the caller's last mirror read and the SIGKILL
rides the returned block instead of dying silently with the process.

When that wait **expires** — an idle-start send holding the lock through its turn-confirm — stop
must still win, so it refreshes and hammers anyway. That message is **stopped and never runs**.
Whether it also comes back to the composer depends on a race: if its enqueue landed before the
best-effort re-capture it rides the returned block; if it landed after, in the dead epoch, it does
not. `conservation_storm_test.py` accepts **both** shapes deliberately on its slow-send branch,
because on a heavily stalled machine the lock holder can release just inside the wait and the base
drain then captures the message under the lock.

So on this one branch a message can end neither Delivered nor Returned — it is stopped. That is
the deliberate "stop must win" posture (matching pi and codex, which never hold the lock at all),
not an accident.

### E3. pi and claude — the queue and the transcript ride different transports (A3b ordering)
A3b requires depart-before-arrive: the chip is removed **first**, then the transcript turn
appears. Both harnesses emit in that order — it is the ordering we control.

But the two updates reach the browser over **different transports**: the queue snapshot goes over
the agents WebSocket, the turn over the per-agent event stream. A rare transport reordering can
still land them out of order, so a single redraw can briefly show the message as both a chip and
a turn. Millisecond-scale and self-correcting on the next update; there is no double-delivery,
only a momentary double-show. Closing it fully would need both updates on one transport, or a
sequence number the frontend orders on — neither is built.

### E4. pi — `ctx.abort()` drains the queue into an unreadable editor
`ctx.abort` routes to `_extensionAbortHandler` →
`restoreQueuedMessagesToEditor({abort: true})`. It empties pi's native steer queue into the TUI
editor buffer **unsent**, then aborts. The extension's pi API (`sendUserMessage`, `setModel`,
`setThinkingLevel`) cannot read that editor buffer, and the extension keeps no copy of the steer
text — it fire-and-forgets from `pi_inbox`. So "abort, then resubmit the queued steers" has
nothing to resubmit.

Consequence: pi's interrupt cannot be payload-free. The extension must own the payload — re-read
the specific `pi_inbox` lines for the parked steers and resubmit them once the abort settles to
idle, without double-submitting the copy already sitting in the editor. `agent.ts` also re-queues
via `prompt()` while `isStreaming` is still true, so a naive resubmit immediately after abort just
re-parks the steers, stranded until the next user prompt.

A turn counter used for ABA-safety must be **persisted across restart**: a fresh process resets
it, and a naive reset re-aliases turn id N onto a different turn.

### E5. codex — the daemon does not enforce service tiers
`thread/settings/update` keeps whatever `serviceTier` is set, even on a model that does not
support `priority`. The daemon will not reject or clear it. So the guarantee "a no-fast model has
no fast, and clearing it works" is **frontend-enforced**:

- `supports_fast` must come from `model/list`'s per-model `service_tiers`, never a static table.
- A model switch must write **all three axes** (`model` + `effort` + `fast`) together, not only
  the diffed ones, so a stale `priority` cannot survive a model change.

Related: `model/list` is per-account *and* per-model. Efforts differ per model (some `low→ultra`,
some `low→max`, some `low→xhigh`) and `service_tiers` is non-empty only on some families. **A
static uniform catalog cannot represent this** — which is why the catalog is daemon-sourced.

### E6. codex — hook trust is not bypassable on the resume path
`codex resume <id> --remote` stops on a "Hooks need review" screen and **ignores**
`--dangerously-bypass-hook-trust`. Until trust is granted, no hooks fire on any turn, typed or
programmatic. `wait_for_ready_signal` therefore selects "Trust all and continue" once at create
time (send-keys `2` on the primary window); codex persists that under `CODEX_HOME`, so it is
one-time and `start`/`connect` never see the screen.

The daemon must also launch as `codex --dangerously-bypass-hook-trust --enable hooks app-server`
— hooks are a default-off feature flag, so a bare `app-server` fires none.

### E7. codex — programmatic turns fire no transcript hook
`codex_transcript_path` is written by mngr rather than derived from a hook, because a
programmatic `turn/start` does not fire the hook that would otherwise record it.

### E8. codex — `turn/started` / `turn/completed` are not emitted by 0.147
The app-server stopped emitting those notifications; only `thread/status/changed` remains. The
activity dot therefore follows mngr's authoritative RUNNING state via `CodexActivityTracker` (the
same lifecycle+transcript path as claude and pi), not the ledger's turn notifications. The ledger
stays the queue/message-lifecycle authority. Deriving the dot from the ledger's notifications is
what stuck it on "Thinking".

### E9. Test-environment quirks (not product bugs)
- `test_codex_agent_full_lifecycle` is functionally green end-to-end but exits non-zero locally on
  the resource-guard mark check (`@pytest.mark.tmux` / `rsync` "marked but never invoked"): in a
  sandbox those binaries do not route through the guard's PATH wrapper. In CI the wrappers are
  active and the marks pass. The same cause makes several `adopt`/`destroy` unit tests "fail"
  locally.

### E10. Deliberately not closed
- **claude model/effort switches do not survive restart.** Launch settings re-pin the model every
  relaunch; only `fastMode` is recorded per-agent. Fix is to record model+effort in the same
  per-agent settings file. (The restart precedence here was inferred, not observed — verify
  against a real restart before acting on it.)
- **pi has no create-time model/effort knobs**, and its switch can report success even when the
  extension drops the model.
- **Create-time model selection is three unrelated mechanisms** across the harnesses with no
  shared mngr abstraction. Unify when next touching this area.
- **codex switch durability across `codex resume` is unverified.**
- **`initial_message` delivery as a first `turn/start` is unimplemented** for codex. Rarely
  exercised.
- **AGENTS.md injection renders as a giant fake user message** in codex common transcripts;
  instruction-injection turns are not yet tagged or skipped by the converter.

### E11. antigravity — A3 identity is unreachable; the displayed queue is OURS
**Structural, not a bug to fix.** Every other harness's queue is observable: codex's pending
steers, claude's parked send queue, pi's inbox. agy's parked messages live inside its TUI and
touch no file, so there is nothing to mirror. The design therefore inverts A3: agy is never
allowed to park anything, so the queue we display becomes the only queue that exists.

"Never allowed to park" is enforced in exactly one place. `session.send` does not type at all —
it enqueues — and a single flush worker is the only code that ever delivers, typing only when
its bounded predicate says no turn is open. An earlier design let both the session and the
worker type, and decided per-send whether to; that decision was a check-then-act whose window
was wide enough to land a message in a turn that had just started.

Two residuals remain. **Other senders:** a message typed into agy's own terminal, or `mngr
message` from cron, opens a real turn we neither see nor hold; we wait it out, and the turn it
opens is visible in the transcript, so it correctly holds our queue. **The handoff window:**
between our last observation and agy's input handler taking our Enter, another actor can open a
turn and our block merges into it — see E12.

### E12. antigravity — delivery is observed, not acked, and the ambiguous verdict resends
A4 wants a backend-minted id checked against the committed transcript. agy's protobuf transcript
carries no client id, and the only way to put one there would be to smuggle a marker into the
user's text, which the model would see. So delivery is decided by observation.

mngr's own ack cannot be that observation. Its sole submission probe is agy's `active` marker
mtime, which `statusline.sh` touches on every busy sample — so a message that merely PARKED in
agy's composer acks as delivered. Our proof would be true during the exact failure it is meant
to detect. The verdict is instead: does agy's own store gain a user turn whose text is our
block? That needs no id, and it works for turns we did not send.

**"One block, one turn" is measured, not assumed** (agy 1.1.20): `tmux send-keys -l` with an
embedded newline INSERTS a newline in the composer rather than submitting, and a single Enter
then commits the whole block as exactly one `USER_INPUT` row. A prefix arm survives in the
verdict as defence in depth, not because a partial submission has ever been seen.

The residual is the ambiguous verdict. If no matching row appears we cannot distinguish "still
parked in the composer" from "merged invisibly into someone else's turn", so we retry — which
means the merge case delivers the text twice. We choose duplication over loss deliberately: a
duplicate is visible and correctable, a swallow is neither. Retries are bounded (three
attempts), after which the entry stops being retyped and stays on screen as failed, so the
duplication is bounded too.

Consequence for A6: during a flush the claimed entries read "Sending…" for the remainder of the
send while the turn is already generating. Dropping them at hand-off instead would blink them
off screen before the turn renders, which A1a forbids outright and which E1 records as the
worse failure. Same shape as codex's shoulder-tap resend, same render path.

### E13. antigravity — the cancel key is a single ctrl+c, and a double press exits
agy binds `cli.escape` to both `esc` and `ctrl+c`. `esc` carries text-editing meaning in too
many TUI contexts to deliver blind; `ctrl+c` is unambiguous on the FIRST press, but agy treats a
double press as exit and its docs say that valve fires regardless of remapping.

A greyed button is not sufficient protection for the only failure here that destroys the agent
process, so both callers press through a shared per-agent interlock that refuses a second press
inside a fixed window — per agent, not per caller, so stop and a tap racing cannot between them
deliver the pair. Stop falls back to the restart when a press is refused or does not settle; the
tap does not restart, it reports and leaves the queue intact.

A dedicated chord bound to `cli.escape` (claude's approach) would remove the ambiguity and
remains the right end state; it is blocked on confirming where agy reads `keybindings.json`,
since a write to the wrong path would silently no-op and leave stop and tap dead.

### E14. antigravity — a delivery that cannot be witnessed stays Queued, then fails visibly
A flush whose block never appears as a user turn re-queues it and retries on the next wake, up
to three attempts. After that the entry is no longer retyped: it stays visible as a queued
message rather than being delivered or returned. Conservation holds — it is in exactly one
state and always on screen — but Part B's "eventually either Delivered or Returned" is
satisfied only by the user stopping the agent, which returns it to the composer.

The alternative is worse: an unverifiable delivery retried without bound re-pastes the whole
block on every attempt, so a message agy did receive arrives N times.

### E15. antigravity — the turn-open predicate is bounded, so it can be wrong in both directions
Every rung of "is a turn open?" carries a freshness bound, because an unbounded rung cannot
terminate. Measured on agy 1.1.20: a single ctrl+c during a tool call settles that step as
`CANCELED`, and the parser emits a `tool_result` for it, so the transcript tail reads "open"
forever afterwards. A predicate that trusted the tail alone would make the first stop an agent
receives hold every later message for the life of that agent — a silent per-agent outage, worse
than the swallow it replaced.

The bounds are therefore load-bearing, and they are guesses: a busy-marker freshness window and
a maximum age for an open-looking tail. Too short and a genuinely long tool call is typed into;
too long and a wedged agent takes that long to recover. Our own cancels are stamped exactly
rather than inferred, so the common case needs no bound at all; the ceiling only covers an
abandonment we did not cause and cannot see.

### E16. all harnesses — a "Sending…" can persist through a catastrophe, and nothing rescues it
**Accepted deliberately.** The frontend has no timer and never resolves its own placeholder
(A2), so whatever the backend last reported is what stays on screen. Every bound therefore
lives in the backend, and if the backend stops *reporting* — rather than stops working — the
message reads "Sending…" until something republishes.

In the ordinary failure modes this is bounded and self-correcting:

- **claude / pi / codex:** the send blocks in-request, mngr confirms within
  `DEFAULT_CONFIRMATION_TIMEOUT_SECONDS` (plus the TUI-ready wait), and the session resolves
  its registry entry in a `finally` — so a failed send returns the message within ~2 minutes
  whether or not mngr raised.
- **antigravity:** the send is off-request, so the bound is the worker's: a bounded send, a
  bounded delivery witness, and an attempt ceiling after which the entry stops being retried
  and reads *Queued* rather than *Sending*. Its worker also republishes the queue on every
  tick, level-triggered, so a stale snapshot is corrected as soon as any watcher exists.

What is NOT bounded is the case where nothing is left to report: a watcher torn down while a
send is still in flight (its publisher is detached, so the settle updates the queue but
announces it to no one), an agent never re-watched afterwards, or a worker thread lost to
something its handler cannot catch. The state is correct in the backend; only the last thing
the UI heard is wrong.

**Why there is no backstop.** A staleness timer that demoted a stuck "Sending…" would make the
UI look right while the send path stayed exactly as unreliable — paying the symptom, not the
debt (A2). The honest fix for a message stuck in this state is to make the send robust enough
that it cannot get there, and the honest cost meanwhile is a confusing UI in a catastrophe. A
reload re-reads real state; stopping the agent returns the message to the composer.

The scope is deliberately minimal: this is reachable only when the backend keeps running but
stops publishing for a given agent, which is not a state ordinary failures produce.
