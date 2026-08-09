# `shoulder_tap_atomic` for pi — minimal spec (post-adversarial-review)

Status: DESIGN. Companion to `docs/design/shoulder_tap_atomic.md` (codex, shipped).
Rewritten after three adversarial reviews collapsed the first draft ~4x. The earlier draft
(turn counter + `pi_turn_state.json` + payload + outcome marker + second poller) was almost
entirely an artifact of one wrong early choice. This is the minimal version.

## 0. First, the honest scope question (decide this before building)

**pi already ships a working shoulder-tap.** We deliver mid-turn messages as
`deliverAs:"steer"`, and pi's agent loop injects steers greedily before the next model
response and re-polls after **every** tool-call round (`agent-loop.ts:167,182-190,259`). Tool
boundaries arrive every few seconds, so greedy steering already covers ~90-95% of cases at
zero cost. The atomic interrupt's *only* marginal win is **cutting one long, uninterrupted
generation / long thinking block short** (no tool call to provide a boundary) — and even then
it trades "keep the partial, act at the next boundary" for "discard the partial, act now" (the
partial isn't committed to the transcript, same as codex §5).

The restart-flush this "replaces" was never pi's path anyway — that fallback exists for
harnesses without native steer (claude). So the real comparison is **atomic-interrupt vs.
greedy-steer-that-already-ships.**

**Decision 1:** ship the ~40-line version below purely as latency polish for the long-thinking
case, or declare greedy steering sufficient for pi and close this. (My lean: the small version
is cheap enough to ship; it is not urgent.)

## 1. The minimal mechanism (design A — read the editor back)

The key realization from review: **do not have Minds hand back the queued text.** When
`ctx.abort()` runs, pi *synchronously* drains its own parked steers into the TUI editor, and
the extension can read them straight back. So the extension is self-sufficient — no payload,
no turn counter, no state file, no outcome marker.

Chain (all confirmed against source):
- `ctx.abort()` → `_extensionAbortHandler` synchronously (`agent-session.ts:2420-2423`) →
  `restoreQueuedMessagesToEditor({abort:true})` (`interactive-mode.ts:1819-1824`) →
  `clearAllQueues()` (empties the steering queue) → `editor.setText(<steers concatenated>)` →
  `agent.abort()` (`interactive-mode.ts:4209-4227`). mngr runs pi in interactive TUI mode, so
  this handler is always bound (`plugin.py:509,616-667`).
- The held `latestCtx` is a live `ExtensionContext`; `ctx.ui.getEditorText()/setEditorText()`
  are wired to the live editor (`types.ts:307-338,216,219`; `interactive-mode.ts:2367-2368`).
- `isIdle === !isStreaming === !_isAgentRunActive`, and it stays **false for the whole user
  turn incl. tool rounds and internal continuations**, true only when fully settled
  (`agent-session.ts:597,883-884`). So `isIdle()` is the correct "safe to resubmit" signal.
- `pi.sendUserMessage(text)` to an **idle** agent starts one fresh turn; a single concatenated
  string = one turn (`agent-session.ts:1167-1180`; `agent.ts:390-407`).

### The code (one file, ~25 lines)
In `mngr_pi_lifecycle.ts`, reuse the existing `pi_inbox` drain (append-only, byte-offset
tailed, already holds `latestCtx`, already on a 200ms timer — `:395-435`). Accept one control
record shape alongside the existing string-message case (`:415`):

1. On `{ "minds_interrupt": true }`, in ONE synchronous tick (no `await` between — the
   atomicity guarantee). NOTE: `restoreQueuedMessagesToEditor` *appends* the drained steers to
   whatever text is already in the composer (`interactive-mode.ts:4220-4221`), so we must
   clear-then-restore around the drain, or a user's composer draft would be resent too:
   ```
   const ctx = latestCtx;
   if (ctx && !ctx.isIdle()) {
     const draft = ctx.ui.getEditorText();  // usually "" in Minds (you type in the web chat)
     ctx.ui.setEditorText("");              // clear so the drain lands into an empty box
     ctx.abort();                           // pi appends its real queue into the now-empty box
     resubmitText = ctx.ui.getEditorText(); // == exactly the queued steers, nothing else
     ctx.ui.setEditorText(draft);           // restore the user's draft; box left as it was
     inboxPaused = true;                    // serialize: hold further inbox injects until resubmit
   }
   ```
   The resent text is thus sourced from pi's *own* queue (authoritative, no Minds-view lag),
   the composer draft is preserved, and no stray draft can leak into the shoulder tap.
2. In the same timer body, once the abort has settled:
   ```
   if (resubmitText != null && ctx && ctx.isIdle()) {
     const t = resubmitText; resubmitText = null; inboxPaused = false;
     if (t.trim()) pi.sendUserMessage(t, { deliverAs: "steer" });  // one merged fresh turn
   }
   ```
3. While `inboxPaused`, `drainInbox` skips injecting further lines (so a not-yet-injected inbox
   line can't start a competing turn between abort and resubmit).

### Minds wireup (~12 lines)
- `_shoulder_tap_atomic_endpoint` for pi: append one line `{"minds_interrupt":true}` to
  `$MNGR_AGENT_STATE_DIR/pi_inbox` (pi's home is the state-dir root). No turn-state read, no
  content gathering, no activity reset, no restart.
- Flip `native_atomic_shoulder_tap_possible = True` for `PI_CODING` on the **`HarnessCatalog`**
  (both catalog constructions). The button already branches on the flag.

## 2. What review CUT (and why each cut is safe)

- **The Minds-supplied `contents` payload → gone.** Editor read-back reads pi's *actual*
  drained queue, which is always consistent with what pi really had (Minds' view can lag the
  ~200ms inbox pipeline). Also removes the payload asymmetry with codex — pi's intent is now
  payload-free too.
- **The turn counter + `pi_turn_state.json` + restart seeding → gone (see Decision 2).** Under
  read-back a stale tap that hits the *next* turn simply aborts it and resubmits *that* turn's
  own editor content — self-consistent, no duplication. So the ABA counter guards almost
  nothing.
- **The outcome marker → gone.** The resubmit is a plain user message; this extension's
  `message_end` already writes it to the common transcript Minds renders (`:588-621`), and the
  `active` marker already brackets it. Minds reconciles from what it already watches.
- **The second file + second poller → gone.** One record shape on the existing `pi_inbox`
  drain. (`pi_control.json` is the wrong thing to reuse — single-slot last-wins; the inbox is
  append-only, which is what a trigger wants.)

## 3. The ABA gate — optional for pi (Decision 2)

Codex's gate mattered because a stale interrupt could hit turn N+1. For pi under read-back,
hitting N+1 is *self-consistent* (abort N+1, resubmit N+1's own steers) — the only "harm" is
interrupting a turn the user didn't consciously target, and they clicked *because* they saw
something running. So the minimal design uses a bare `!isIdle()` guard, no id.

**If you want the strict "don't touch a turn newer than the one I saw" semantics anyway**
(you insisted on it for codex): it costs the counter back, and review found the naive version
is *wrong* — you must use pi's real per-user-turn boundaries, not the raw loop events:
- Increment on **`before_agent_start`** (fires once per user prompt, `types.ts:698-709`), NOT
  `agent_start` (which fires per internal loop — retry, compaction, queued continuation —
  `agent-loop.ts:109,138`; `agent-session.ts:1067-1104`). Using `agent_start` over-counts
  mid-turn and silently drops taps exactly on the long turns this targets.
- Write `running:false` on an **`agent_settled`** / true-idle transition, NOT `agent_end`
  (which also fires per loop, while `isIdle` is still false).
- Seed the counter from the parsed last-turn value, NOT `countLines()` of a single-object file
  (that resets to 1 on restart and re-aliases ids).

My recommendation: **skip the gate** (bare `!isIdle()`); the counter's cost isn't worth pi's
tiny, low-harm race. But it's your call given the codex precedent.

## 4. Bugs review caught (already folded into §1)

- **"Resubmit on `agent_end`" is broken** — at `agent_end`, `_isAgentRunActive` is still true,
  so `isIdle()` is false: an `isIdle`-gated resubmit never fires, and an ungated
  `sendUserMessage` throws "Agent is already processing" and the message is dropped
  (`agent-session.ts:597,1167-1172`). Use the `isIdle()` poll only.
- **Double-delivery vs the inbox timer** — solved by putting the trigger *in* the ordered
  `pi_inbox` (so all prior messages are seen first) **and** the `inboxPaused` serialization, so
  a line can't be both resubmitted and re-injected.
- **Editor copy is inert** — `restoreQueuedMessagesToEditor` never submits; in headless mngr
  nobody presses Enter, so the drained editor text is harmless (we clear it anyway).

## 5. Open implementation risks (verify while building)

1. **Async-enqueue timing (the one real subtlety).** `pi.sendUserMessage` is async; a steer
   injected microseconds before the interrupt may not be in the steering queue yet when
   `ctx.abort()` drains it, so read-back could miss it. Because the trigger sits *after* those
   messages in the same ordered inbox, they were already `sendUserMessage`-called — but confirm
   they've actually enqueued (they steer to a *running* turn synchronously enough), or add a
   one-tick settle before reading the editor. This is the thing to nail in code.
2. **Held-ctx staleness in a reload/`/new` window** — `ctx` accessors throw once the runner is
   invalidated (before the next `session_start` refreshes `latestCtx`); a tap in that window is
   caught by `safe()` and silently dropped. Acceptable (rare), but note it.
3. **Empty-queue guard** — handled by the clear-then-restore in §1: we clear the box before
   the drain, so with nothing queued the post-abort box is `""` (→ `resubmitText.trim()` empty
   → resubmit nothing, a bare interrupt) and the user's draft is restored regardless.

## 6. Deployment (the whole point)
No pi fork, no `build.sh`, no EC2, no release, no sha-pinned reinstall — it's a TypeScript
change to the extension we already vendor, plus the Minds endpoint + flag. Total ~40 LOC.

## 7. Transcript parity
Interrupting pi mid-generation loses the partial from pi's session file, so Minds shows nothing
for the interrupted attempt — identical to codex, and no net loss vs the old restart (as we
confirmed). Completed content before the interrupt is still in the transcript.

## 8. Build order
1. Extension: the `pi_inbox` `{minds_interrupt:true}` branch — abort + read-back + clear +
   `isIdle`-gated resubmit + `inboxPaused` serialization. Nail risk §5.1.
2. Minds: pi branch of `_shoulder_tap_atomic_endpoint` (append the sentinel) + flip the flag.
3. Live E2E on a real pi agent: long thinking turn, queue 3, tap, confirm one merged turn, no
   dupes, no drop.
