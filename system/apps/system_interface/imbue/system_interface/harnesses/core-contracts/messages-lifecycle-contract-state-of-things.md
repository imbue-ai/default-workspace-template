# Message lifecycle: the state of things

How each harness currently satisfies `messages-lifecycle-contract.md`, and every gap that
remains open. The contract beside this file is timeless; everything here is a snapshot and
is expected to churn.

**If you close a limitation, delete its entry.** An entry that is no longer true is worse
than no entry, because the next person plans around it.

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

---

## pi — remaining gaps

These were a standalone file next to the pi harness; they live here now so every harness's
gaps sit in one place. None of the first three lose or corrupt a message; they are brief visual
transients the claude harness exhibits too. The fourth is a genuine but rare loss, deferred
deliberately.

## 1. Queued -> Delivered can briefly double-show (contract A3b)

**What:** when a queued message drains into a committed turn, the queued chip should disappear
*before* the transcript turn appears. The backend emits the chip-removal first (see
`watcher.py: _emit_unsent`), but the two travel to the browser on **different transports** --
the queued-message snapshot on the agents WebSocket, the transcript turn on the per-agent SSE
event stream. So under transport jitter the turn can paint for a redraw or two while the chip is
still up (the message shown as a chip AND a turn at once).

**Frequency:** rare. It requires the SSE frame to overtake the WebSocket frame despite being
emitted second, and only when you are actually using the queue (typing while pi is mid-turn).
When it happens it is a sub-second flicker that self-heals on the next redraw.

**Why accepted:** the only fully-correct fix is to co-emit the chip-removal on the *same* channel
as the transcript turn (one ordered stream) or to reconcile chip-vs-turn by a shared id on the
frontend. The id path was explicitly rejected (we do not mint correlation ids; see the contract
doc's Part C notes and the harness discussion). The claude harness has the identical two-transport
structure and the identical residual, so this is at parity, not a regression.

## 2. A "Sending..." bubble can clear a beat early (contract A1a / A2 positional correlation)

**What:** the optimistic "Sending..." bubble is removed positionally (oldest-first) when any real
user turn or queued chip arrives (`frontend/src/models/OutgoingMessages.ts: noteBackendArrivals`),
not by matching the specific message. So if a user turn commits from **another browser** or from
the **agent's TUI** while your own send is still in flight, your oldest bubble can be dropped
before its own real representation appears -- briefly showing nothing for that message until its
true state arrives.

**Frequency:** rare, and impossible for a single user in a single tab: it needs a *second*
concurrent writer (another browser on the same agent, or the TUI) committing a turn inside the
sub-second window your send is in flight. Brief and self-correcting.

**Why accepted:** the contract explicitly blesses positional oldest-first correlation with
arrival-id dedup ("over-eager removal is harmless -- the real bubble is what shows"). Making it
exact would require threading a minted message id through `pi_inbox` and pi's session record --
the same rejected id-minting as limitation 1. The claude harness correlates positionally too.

## 3. "Sending..." and "Thinking..." can briefly co-show on an idle-start send (contract A6)

**What:** when you message an idle pi agent, the turn's activity ("Thinking...") and the removal
of the "Sending..." bubble ride different transports, so for a redraw or two both can be on
screen.

**Frequency:** rare, cosmetic, sub-second, idle-start only. Pre-existing (not introduced by the
lifecycle work) and present for the other harnesses.

**Why accepted:** cosmetic transient; no message is affected. Fixing it is the same
single-channel / id-reconciliation work as limitation 1.

## 4. Interrupt during a shoulder-tap flush can lose the flushed messages (contract Part D) -- DEFERRED

**What:** this is the one genuine message *loss*, and the only limitation here that is not merely
a visual transient. During a shoulder-tap flush, the mngr pi lifecycle extension
(`system/vendor/mngr/libs/mngr_pi_coding/.../mngr_pi_lifecycle.ts`) aborts pi's live turn and
holds the captured steer messages in an in-memory `pendingResubmit` while it waits for idle to
re-inject them -- and the dwt queue mirror has already been cleared by the flush sentinel. If a
Stop (retract) lands in that window, the backend reads an empty mirror (returns nothing to the
composer) and the extension then discards the resubmitted steers -- so those messages are lost:
neither Delivered nor Returned.

**Frequency:** rare. You must hit Stop within the fraction of a second between tapping Shoulder-tap
and the extension re-injecting the flushed messages.

**Why deferred:** unlike 1-3, the fix is not in this app -- it is in the mngr extension (make a
retract during a pending resubmit reclaim and return those steers rather than discard them), which
is a `libs/mngr` change with its own changelog and test discipline. It also cannot be caught by the
in-process storm test (`harnesses/conservation_storm_test.py`), whose `_PiWorld` models the
extension as committing flushed steers instantly and so has no `pendingResubmit` window; proving it
needs a test against the real extension. This is why the claude harness does not have the loss: its
flush is a dwt-owned SIGKILL-restart-drain, so the captured block never leaves the backend's hands.
Fixing pi means either fixing the extension (keep the gentle native flush) or making pi's flush
dwt-owned like claude's (lose the gentle no-restart tap). Decide deliberately when it is picked up.
