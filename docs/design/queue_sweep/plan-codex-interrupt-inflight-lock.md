# codex stop button: close the in-flight-send race (message-lock on dwt + input-fence in the fork)

Follow-up to `plan-codex-interrupt.md`. That plan shipped codex's native
interrupt-and-retract (the `{"retract_turn_id": "<id>"}` line on
`shoulder_tap_atomic.jsonl`, handled by the fork's `trigger_shoulder_tap_retract`, landed
on the fork's `claude-codex-pi-mngr` branch). It named, and accepted, the in-flight race in
its Open risks: *"A steer sent between capture and the tap is injected then
retracted-and-discarded, absent from the handback."* Worse than the plan states, the same
window also lets an in-flight steer land **after** the abort and **start a fresh turn** —
the outcome the user specifically wants gone. This plan closes it. It needs **both** a
dwt-side change and a fork rebuild.

## REVISED AFTER ADVERSARIAL REVIEW — NO REBUILD NEEDED; drop part 2

An adversarial review overturned this plan's central premise. **Codex does NOT need a fork
rebuild.** Part 2 (the input-drain fence) and the semantic-spec update are withdrawn. Only
part 1 (the dwt `message.lock`) is needed, and it closes the turn-running race by itself.

Why part 2 was wrong (all confirmed against the code):

1. **The lock already waits for the steer to PARK, not just for pty delivery — so "axis 2" is
   already closed.** Codex's mid-turn send is STRICT: `InteractiveTuiAgent.send_message` holds
   `message.lock` across `submit_message_and_confirm` and only releases on the queued-input
   sidecar advancing (`tui_agent.py:135-201`; `mngr_codex/plugin.py:433-469`). The fork writes
   that sidecar record *synchronously with the park* — `append_queued_input(...)` immediately
   before `pending_steers.push_back`, same serial tick, no await (`patches/0.146.0.patch`,
   input_submission.rs ~2481-2499). So by the time the drain acquires the lock and appends the
   retract line, S is already in `pending_steers`, and the existing `on_interrupted_turn`
   discard drops it. The body's lines 76-78 ("the lock guarantees keystrokes delivered, not
   parked") are false for codex and contradict this plan's own line 74. The whole rebuild
   justification collapses.

2. **The fence is likely not implementable as described.** `event::poll(Duration::ZERO)`
   inside `handle_shoulder_tap_atomic` cannot reach input the crossterm reader already
   consumed; the patch never touches the input loop, and a poll/read there would steal events
   from the reader thread (crossterm shares one global source) and mis-split bracketed paste
   (Paste text vs the Enter key). PLAUSIBLE only because upstream's run loop isn't in the
   worktree, but the burden is unmet.

3. **The fence wouldn't help the one genuinely-open case anyway.** If turn T *ends* between
   capture and codex processing the paste, the paste opens a NEW turn T′; the retract is
   ABA-gated on T and `decide_shoulder_tap` returns `Ignore(TurnIdMismatch)`, so T′ runs. The
   fence lives inside the retract handler and never fires for T′. This is the inherited
   optimistic-handback window, closed by neither part.

4. **The spec update is a category error.** Putting a cross-process observer-lock obligation in
   a doc scoped to "what the patched binary must do" (re-verified per codex tag) has no tag to
   track and will rot. Withdrawn.

5. The proposed `minds_` gate test would pass today (a steer already in `pending_steers` is
   discarded by the existing path), giving false assurance. Withdrawn with the fence.

Remaining real issue that part 1 DOES introduce, same as the pi sibling: **a Stop-button hang
of up to the send-confirmation timeout**, because the STRICT send holds `message.lock` through
confirmation and the drain now blocks on it (`tui_agent.py:135-201`). Same fix menu as the pi
plan: hold the lock across the durable park only (mngr change, benefits both harnesses), or a
bounded drain-side wait with base-restart fallback. Also verify the lock-path identity
(`agent_state_dir/message.lock` on both sides; codex state is under `plugin/codex/home` but the
lock is at the root).

Net: this becomes a **dwt-only** change (plus the shared lock-hold-scope fix), NO fork rebuild,
NO sha repin, NO spec edit. Ship part 1; delete part 2 and the spec section below when
finalizing.

## Contract being enforced

Contract B, sharpened (same wording as the pi sibling): Stop returns to the composer every
message queued *or in flight* when it was pressed, and leaves nothing running. Codex's
queued half holds; the in-flight half races on two axes at once (control-file vs paste
ordering, and codex's async park-vs-abort). [Review note: the "async park-vs-abort" axis does
not actually exist for codex — see the revision banner above; the STRICT send closes it.]

## The race (precise, against the shipped retract)

Two concurrent Flask requests on one agent:

- `POST /message` → `send_message_to_agent` → `CodexAgent` (an `InteractiveTuiAgent`) pastes
  the steer into codex's tmux pane (`base_agent.send_message`, under `self._message_lock()`,
  send-keys + Enter). "sending…" spans this.
- `POST /drain-to-composer` → `CodexInterruptToComposer.drain_to_composer`
  (`harnesses/codex/model.py:147`): `get_all_events`, capture `block`, compute
  `current_open_turn_id`, append `{"retract_turn_id": T}` to `shoulder_tap_atomic.jsonl`,
  `clear_queue`. **No `message.lock`.**

Inside the fork (verified on `claude-codex-pi-mngr`): a std OS thread tails the control file
(~150 ms poll) and posts `AppEvent::ShoulderTapAtomic { intent }`; the crossterm input
reader posts terminal-key AppEvents; both feed **one** serial app-event loop.
`handle_shoulder_tap_atomic` reads the live turn id (one `.await`), then synchronously
decides and calls `trigger_shoulder_tap_retract`, which arms
`discard_pending_steers_after_interrupt` and submits `Op::Interrupt`; `on_interrupted_turn`
later drains `pending_steers`, appends `queued_retracted` per id, and drops the text.

The window: the steer S is parked only when codex processes the pasted Enter AppEvent. Two
orderings decide S's fate:

- **Enter processed before the retract's `on_interrupted_turn`** → S is in `pending_steers`
  when the discard runs → S discarded (`queued_retracted`), never runs. Good — *if* the
  handback also carried S.
- **Enter processed after the abort (codex idle)** → S opens a **new turn** and runs. The
  ABA gate does not help: the retract was gated on T; S's new turn has a new id. **S runs
  anyway.**

Two independent things make the bad ordering reachable, and the current design closes
neither:

1. **dwt side.** The retract line is appended with no lock, so it can hit disk before the
   paste's Enter is even delivered — the control-file poll then fires the abort first.
2. **fork side.** Even if the paste lands first on the wire, the crossterm reader thread and
   the control-file tailer race to post onto the serial loop; nothing guarantees the Enter
   AppEvent is enqueued (and thus S parked) before `ShoulderTapAtomic` is processed.

Closing the race needs a fix on each axis; neither alone is sufficient.

## Fix, part 1 — dwt (orders the two writers)

Same change as the pi sibling (`plan-pi-interrupt-inflight-lock.md`): acquire mngr's
per-agent `message.lock` in `CodexInterruptToComposer.drain_to_composer`, and capture the
block **under** it, before appending the retract line. Reuse `agent_message_lock`
(`harnesses/claude/tap.py:138`; lift it to `harnesses/interrupt.py` since three harnesses
now share it).

Effect: acquiring the lock blocks until the in-flight paste has completed (mngr holds the
lock across send-keys + Enter), so the retract line is appended strictly **after** codex has
received the keystrokes, and the block capture — run under the lock, refreshed via
`get_all_events` — includes S once codex has recorded its `queued_input`. This removes axis
1 and shrinks axis 2's window from "the whole `POST /message`" to "codex's own paste→park
latency."

But axis 2 is *not* fully closed by the lock: `message.lock` guarantees the keystrokes are
*delivered to the pty* before the retract line exists, not that codex has *parked* them.
The two fork threads can still reorder. Hence part 2.

## Fix, part 2 — fork (orders park-vs-abort on the serial stage)

Give the retract handler a **drain-input-before-abort fence**: on the serial stage, before
`trigger_shoulder_tap_retract` submits `Op::Interrupt`, drain all terminal input that is
already available into codex's normal input path, so any steer whose keystrokes were
delivered before the retract line was appended is parked into `pending_steers` first — and
therefore discarded by the very interrupt that follows, instead of surviving to open a new
turn.

This is the codex analog of pi's tick-deferral (`mngr_pi_lifecycle.ts:628` — never consume a
sentinel in a tick that already injected a string; let the steer park first). It stays
entirely on the single serial event stage, so the existing atomicity guarantee (no
turn-lifecycle event interleaves between the id read and the act) is preserved: the drain is
synchronous, with no `.await` between it and the compare-and-trigger.

Concretely (0.146.0; version-agnostic contract below): in `handle_shoulder_tap_atomic`, when
the intent is a Retract, first pump any ready crossterm events through the same handler the
main loop uses (a bounded, non-blocking drain: read while `event::poll(Duration::ZERO)` is
true), so a just-submitted steer reaches `pending_steers.push_back` before the turn-id read.
Only Retract needs it — a Flush already resubmits the parked steers, so a late steer merging
in is harmless; a Retract discards, so a late steer must be captured first or it escapes.

Residual after both parts: the sliver where the pasted Enter is still in the kernel pty
buffer and the crossterm reader has not surfaced it to `event::poll` yet. Under the dwt lock
this is bounded by local-pty delivery latency (microseconds on the same host, where the
system interface runs), versus the full multi-hundred-ms `POST /message` today. Document it
as accepted; it is orders of magnitude smaller and never larger than today's window.

## Suggested semantic-spec update (fork `VERSION-AGNOSTIC-SEMANTIC-SPECIFICATIONS.md` §6)

The current §6 Retract clause guarantees atomicity only between the **turn-id read** and the
**action**. It says nothing about input already delivered but not yet parked, which is
exactly where an in-flight steer escapes. Add an ordering guarantee to the Retract bullet
(version-agnostic — no function names):

> - **Retract** — if `retract_turn_id` equals the currently live turn id **and** a turn is
>   running → **first drain any terminal input already delivered to the process into the
>   steer queue**, so a steer whose keystrokes arrived before this control line was appended
>   is parked (and thus retracted) rather than surviving to open a new turn — then interrupt
>   the turn and discard every entry the interrupt would otherwise restore into codex's
>   composer … [rest unchanged].

And extend the atomicity paragraph:

> The retract's input-drain, the turn-id read, and the compare-and-trigger all run on the
> single serial event stage with no suspension point between them. Draining already-delivered
> input before the abort is part of the correctness guarantee, not an optimization: a retract
> that aborts first and parks a straggler steer afterward will start a turn the user asked to
> cancel. **This pairs with an external ordering obligation on the observer:** the retract
> control line must be appended only *after* the process has been handed the keystrokes of any
> send that was in flight when the retract was decided (the observer serializes its send and
> its retract on the same per-agent lock). The binary closes the in-process reorder; the
> observer closes the cross-process one. Neither alone is sufficient.

The observer-obligation sentence is worth stating in the spec even though it is the observer's
job: it is the contract that makes the binary-side fence *sufficient*, and a future observer
that drops the lock would silently reopen the race with a fully-correct binary.

Add one `minds_*` test to the required set (the build gate is `cargo test -p codex-tui --lib
minds_`): `minds_shoulder_tap_retract_discards_a_steer_delivered_before_the_line` — a steer
whose input is queued on the stage *before* a matching retract is processed resolves to
`queued_retracted` and does **not** start a new turn.

## Ship mechanics

- **Fork** (`.external_worktrees/codex-in-minds`, branch `claude-codex-pi-mngr`): regenerate
  `patches/0.146.0.patch` + `-patch-details.md`, update the spec as above, add the new
  `minds_` test, rebuild both arches via `./build.sh --version 0.146.0` (the `minds_` gate
  must pass), update `SHA256SUMS`/README sums, publish a new release tag. Rebuild REQUIRED.
- **dwt** (`claude-codex-pi-dwt`): the `message.lock` change in `harnesses/codex/model.py`
  (+ the `agent_message_lock` lift) and `server_test.py` coverage; bump `CODEX_PATCH_RELEASE`
  and the two `codex_patch_sha256` pins in `system/scripts/setup_system.sh` to the rebuilt
  assets. dwt changelog entry; the fork needs its own repo changelog per its convention.
- The dwt lock change is independently shippable and strictly improves today's behavior even
  before the rebuilt binary is pinned (it removes axis 1). Land it first; land the pin bump
  with the rebuilt binary. Order: dwt-lock → fork-rebuild+repin.

## Load-bearing assumptions to verify before landing

- **Lock-path identity** (as in the pi plan): `agent_info.agent_state_dir / "message.lock"`
  must equal codex's `_get_agent_dir() / "message.lock"`. codex writes state under
  `plugin/codex/home` (CODEX_HOME) but its mngr agent dir / lock root should be the state-dir
  root; confirm before trusting serialization.
- **crossterm drain is safe to run synchronously in the handler** without double-dispatching
  an event the main loop would also read. The bounded `poll(ZERO)` drain must consume-and-
  dispatch, not peek, and must route through the identical submission path so ledger records
  and thread routing are unchanged. Verify against 0.146.0's input plumbing when implementing.
- **Flush is genuinely unaffected.** The fence is Retract-only; assert a Flush path with a
  late steer still merges it (no behavior change, existing `minds_shoulder_tap_atomic_*` tests
  keep passing).

## Open risks

- **Version skew during the two-step landing.** Between the dwt-lock landing and the
  pin bump, the deployed binary has no fence: axis 2 stays open (shrunk, not closed). That is
  strictly better than today and self-heals when the pin lands. Named, accepted.
- **Kernel-pty sliver** (above): the one window the dwt lock + serial fence cannot close;
  bounded by local-pty latency, documented as accepted.
- **A user typing continuously into the pane.** The bounded `poll(ZERO)` drain consumes only
  what is already ready and returns; it cannot be held open by a fast typist, so the retract
  is not starvable. (A drain that blocked until quiescent would be — do not use one.)
- **Old-binary fail-safe unchanged.** A binary without the fence still treats the retract as
  today (skips it if it also predates retract; runs the plain retract if it has retract). No
  new skew class introduced.

## Non-goals

- pi and claude (separate plans). The pi in-flight fix is dwt-only; claude's empty-queue
  chord already locks.
- Re-plumbing codex message delivery onto the control-file channel to unify the two channels
  (would remove axis 2 by construction but is a far larger change; the fence is the minimal
  fix that honors "no new protocol request, no core change").
- Any change to Flush semantics, the section-4 ledger, or `/interrupt`.
