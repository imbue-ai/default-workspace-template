# `shoulder_tap_atomic` for pi — deep spec

Status: DESIGN (no code yet). Companion to `docs/design/shoulder_tap_atomic.md` (the
codex one, now shipped). This ports the same capability to pi.

## 0. The headline: pi needs NO fork and NO binary build

Codex required forking the Rust binary (patch + EC2 rebuild + release + pinned reinstall),
because the interrupt path lives in codex core. **Pi does not.** Pi's entire lifecycle is
already driven by an extension we own and ship — `mngr_pi_lifecycle.ts`
(`system/vendor/mngr/libs/mngr_pi_coding/.../resources/`) — loaded with `pi -e`. pi's
extension API exposes everything we need (`ctx.abort()`, `ctx.isIdle()`, `ctx.ui` editor
access, `pi.sendUserMessage`, `agent_start`/`agent_end` events). So the whole feature is a
TypeScript change to code we already vendor: no fork, no `build.sh`, no EC2, no release, no
sha-pinned reinstall. Deployment is the normal mngr vendor path.

## 1. What "atomic shoulder-tap for pi" means

Same contract as codex: an external, race-free trigger that interrupts the running turn so
the already-queued messages are delivered now, **gated on the turn the caller observed still
being the live one** (ABA-safe). It replaces the SIGKILL-restart flush for pi (the
`native_atomic_shoulder_tap_possible` flag flips pi to this once it's built).

But note pi's baseline is different: we already deliver messages to a running pi agent as
`deliverAs: "steer"` (shipped), and pi's agent loop injects steers greedily at the next
tool-call boundary. So for pi the *only* thing the interrupt adds is **cutting a long single
model generation / long thinking short** so the queued steer is taken up immediately instead
of after the current model response finishes.

## 2. Three ways pi is harder than codex (and the resolutions)

### 2.1 Pi has no turn id → we mint a turn counter
Codex emits `TurnStarted.turn_id` in its rollout, which Minds already observes; the gate
compared against it. Pi's transcript carries **no turn markers** (`special_kinds` is empty),
only a binary `active` marker. So there is no id to gate on.

**Resolution.** The extension already handles `agent_start` / `agent_end` (it maintains the
`active` marker there). Add a monotonic counter incremented on `agent_start`, and write it to
a new state file `pi_turn_state.json` (`{"turn": N, "running": true|false}`) atomically on
`agent_start`/`agent_end`. Minds reads that file for the gate target — the same "the harness
is the single source of truth for the id it will compare" trick we considered for codex.
Because both the observed id (via the file) and the compared id (in the handler) are the
extension's own counter, the id spaces align by construction.

### 2.2 `ctx.abort()` drains the steers into the editor — it does NOT flush them to a turn
This is the crux, and it's the opposite of codex. In the way mngr runs pi (interactive TUI),
`ctx.abort()` routes to pi's `_extensionAbortHandler`, which interactive-mode sets to
`restoreQueuedMessagesToEditor({abort:true})`
(`agent-session.ts:2420-2426` → `interactive-mode.ts` bindExtensions). That handler:
`clearAllQueues()` (pulls the parked steers OUT of the steering queue), `editor.setText(<the
steers, concatenated>)`, then `agent.abort()` (stops the model stream). So after
`ctx.abort()`:
- the model stream is aborted (good), but
- the queued steers are sitting **in pi's TUI editor buffer, unsent** (in a headless mngr
  agent nobody types Enter, so they never go anywhere), and
- pi's steering queue is now empty.

So unlike codex (whose core merges its own `pending_steers` into one committed turn on
interrupt), pi's interrupt *loses* the queue into an editor buffer. We must re-deliver.

**Resolution — resubmit after abort.** Two viable sources for the text to resubmit; pick one:
- **(A) Editor read-back (self-contained).** After `ctx.abort()`, read the drained text with
  `ctx.ui.getEditorText()`, clear it with `ctx.ui.setEditorText("")`, and resubmit via
  `pi.sendUserMessage(text)` once idle. `ctx.ui` on the event-handler context IS the full
  `ExtensionUIContext` (`types.ts:307,309` → `getEditorText`/`setEditorText` at 216/219,
  wired to the live editor at `interactive-mode.ts:2367`).
- **(B) Caller supplies the contents.** Minds already tracks the pending-steer *content* (it
  renders the queued bubbles). The shoulder-tap intent carries `{target_turn, contents:[...]}`;
  the extension aborts and resubmits the provided contents, and clears the editor
  (`ctx.ui.setEditorText("")`) so nothing stale lingers. This needs no editor read and no
  extension-side queue tracking.

**Recommendation: (B).** It sidesteps the fragile parts of (A) (does a *held* `latestCtx.ui`
still point at the live editor from inside the poll timer? does `getEditorText` reflect the
post-drain state synchronously?) and mirrors how Minds already owns the queued content. It
does mean pi's intent carries a payload, which is an **intentional asymmetry with codex**
(codex is no-payload because its core flushes its own queue; pi can't cleanly flush, so the
owner of the content — Minds — hands it back). Document that asymmetry.

There is **no double-delivery**: `ctx.abort()`'s `clearAllQueues()` empties the steering
queue, and we resubmit exactly the caller's captured contents once; the editor copy is
cleared. If the editor clear is skipped (or unavailable in some ctx), the stale editor text
is benign in the headless flow (mngr injects via the inbox, never submits the editor).

### 2.3 Pi has no "merge into one turn"
Codex's `on_interrupted_turn` merges N `pending_steers` into ONE turn. Pi injects steers
individually. For parity we resubmit the captured contents **concatenated into a single
`sendUserMessage`** (one merged turn), matching codex's user-visible behavior. (Sequential
resubmission is the alternative but diverges from codex; prefer the merge.)

## 3. The mechanism (chosen design)

All in `mngr_pi_lifecycle.ts` + the Minds wireup.

1. **Turn counter + state file.** On `agent_start`: `turn++`, write `pi_turn_state.json`
   `{turn, running:true}` atomically. On `agent_end`/`session_shutdown`: write
   `{turn, running:false}`. (Mirror the existing `pi_model_state.json` write.)
2. **Control channel.** mngr/Minds appends one JSON line to
   `$MNGR_AGENT_STATE_DIR/pi_shoulder_tap_atomic.jsonl`:
   `{"target_turn": N, "contents": ["...", "..."]}`. The extension polls it on a
   `setInterval` (exactly like the existing `pi_control.jsonl` model-switch drain and the
   `pi_inbox` drain — same held-`latestCtx`, same byte-offset tailing).
3. **Handler (atomic gate).** For each new intent, in ONE synchronous tick:
   ```
   const ctx = latestCtx;
   if (ctx && currentTurn === intent.target_turn && !ctx.isIdle()) {
       ctx.abort();                 // stops the stream; drains steers to editor
       pendingResubmit = intent.contents;   // deliver after idle (below)
   }  // else: stale/idle → no-op
   ```
   The check and `ctx.abort()` are one uninterruptible JS tick (no `await` between) — the same
   single-threaded-event-loop atomicity argument as codex. ABA is defeated by the counter:
   if the turn we observed already ended and a new one began, `currentTurn !== target` and we
   no-op.
4. **Resubmit after idle.** `waitForIdle()` is not on the base event-handler ctx, so don't
   await it inline. Instead, once a `pendingResubmit` is set, deliver it when the agent goes
   idle — either by polling `ctx.isIdle()` in the same drain timer, or on the next
   `agent_end` event. Then: `ctx.ui.setEditorText("")` (clear the drained copy) and
   `pi.sendUserMessage(pendingResubmit.join("\n\n"))` (one merged fresh turn), clearing
   `pendingResubmit`.
5. **Outcome marker.** Write a line to a result file (or reuse the queue tracker) so Minds can
   report interrupted-vs-idle, analogous to codex's `queued_committed`/`queued_retracted`.

## 4. Atomicity & ABA correctness (same spine as codex)

- **Atomic:** the extension runs in pi's single-threaded JS event loop. The `if
  (currentTurn===target && !isIdle) ctx.abort()` executes to completion with nothing (not
  `agent_end`, not the loop) interleaving — one "instruction". If the turn ended first,
  `isIdle()` is already true (or the counter advanced) and we skip.
- **ABA-safe:** the gate compares a monotonic generation (the counter), not a bare boolean,
  so a stale intent for turn N never interrupts turn N+1.
- **Counter persistence caveat (learned from codex review):** the counter resets when the pi
  process restarts (mngr resume). Seed it so it never re-aliases an id: persist the last
  counter in `pi_turn_state.json` and resume from it on load (the extension already seeds
  other counters from `countLines(...)` for exactly this reason). A stale pre-restart intent
  then can't match a fresh turn.

## 5. Transcript implications (identical to codex — and no net loss)

When pi's model stream is aborted mid-generation, the partial/streamed output is not committed
to pi's session file, so Minds (which renders from that file) shows nothing for the
interrupted attempt — exactly the codex situation. As we confirmed for codex, the old
restart-flush loses the same partial, so this is **no net regression**; the completed content
before the interrupt is still in the transcript. Not worth chasing unless we later make Minds
consume a live stream.

## 6. Minds wireup (once built)

- Flip `native_atomic_shoulder_tap_possible = True` for `PI_CODING` in
  `harnesses/pi_coding/model.py` (both catalog constructions). The button already branches on
  the flag (shipped for codex).
- The endpoint (currently codex-only, `_shoulder_tap_atomic_endpoint`) generalizes: for pi,
  read `pi_turn_state.json` for the open `target_turn`, gather the pending-steer contents from
  the pi queue tracker, and append `{"target_turn", "contents"}` to
  `$MNGR_AGENT_STATE_DIR/pi_shoulder_tap_atomic.jsonl` (pi's home is the state-dir root, per
  the registered `model_state_relative_path`). No restart, no activity reset.
- Minds already reads `minds_model_state.json` from pi's state dir, so writing/reading these
  sibling files is the same path.

## 7. Open risks / to verify before/with implementation

1. **Held-ctx validity in the timer.** The drain timer uses a captured `latestCtx`. Verify
   `latestCtx.abort()` / `latestCtx.isIdle()` / `latestCtx.ui.setEditorText` still act on the
   live session/editor when called from the timer (the existing model-switch drain already
   relies on held-ctx for `modelRegistry`, which is evidence it's fine, but abort/editor are
   new uses).
2. **`agent_start` granularity.** Confirm `agent_start` fires once per user turn (so the
   counter == "turn"), not once per internal continuation. If it fires more often, key the
   counter on the coarser boundary.
3. **Abort with an empty queue.** Interrupting with nothing queued (just "cut the long
   generation") must be a clean abort with no spurious resubmit.
4. **Payload asymmetry.** pi's intent carries `contents`; codex's does not. This is
   deliberate (§2.2) but the endpoint/flag/docs must not assume a uniform no-payload shape.
5. **Double-submit.** Ensure exactly-once: queue cleared by abort, editor cleared, one
   resubmit. Test the "3 queued → tap → one merged turn, no dupes" path.
6. **Outcome reporting.** Decide the marker so Minds can distinguish interrupted vs
   already-idle (parity with the codex ledger).

## 8. Build order

1. Extension: turn counter + `pi_turn_state.json` (smallest, independently useful).
2. Extension: the `pi_shoulder_tap_atomic.jsonl` drain + atomic gate + `ctx.abort()`.
3. Extension: resubmit-after-idle (merge into one `sendUserMessage`) + editor clear + outcome
   marker.
4. Minds: generalize `_shoulder_tap_atomic_endpoint` to pi (read turn-state, gather contents,
   write control file); flip the pi flag.
5. Verify end-to-end on a live pi agent (send long turn, queue, tap, confirm merged turn +
   no dupes + ABA on a fast turn change).

## 9. Non-goals
- No pi fork / binary build (the whole point — §0).
- No live-streaming of pi partials (separate, like codex §5).
- Claude stays on restart.
