# Plan: End-of-turn progress-view rendering rework

> **Robustly handle all end-of-turn rendering scenarios in the chat progress view, without relying on agents closing steps correctly.**
>
> * **Guiding principle:** the progress view does the right thing whether or not steps get closed; agents shouldn't have to fight the system. Rendering is the load-bearing fix; prompt/hook steering is best-effort only.
> * **Reply detection (backward scan):** scan backward from the end of a turn collecting text-only assistant messages; stop at the first `closed` task event OR tool activity. That trailing run is the user-facing reply, promoted to the top level.
>   * Consequence: "close last step, then write the wrap-up" promotes cleanly (the ideal path). "Speak, then close" leaves the message in-step — accepted, with a deferred escape hatch.
> * **Position-aware top-level messages:** leading text (before the first step) renders *above* the timeline; inter-step text (after a close, before the next step starts) *interrupts* the timeline inline at its chronological position; the trailing reply renders *below* the timeline. No top-level text is ever hidden under a step.
> * **Unclosed steps at idle:** drop the internal-jargon "settled" tag; keep the static partial-ring icon; show no caption when there's nothing to show; any surviving caption is static + italic + muted (no shimmer).
> * **UngroupedWorkBlock:** apply the same backward-scan + leading-above / trailing-below positioning for consistency.
> * **Steering (best-effort):** claude.md guidance = "close your final step *before* writing your wrap-up reply," with a short why. Existing PreToolUse step-creation nudge and non-blocking Stop nudge stay as-is.
> * **Deferred:** a `tk close` flag for the agent to signal "promote my already-given wrap-up" — designed but not built; revisit only if testing shows the speak-then-close case is frequent. No `tk close` flags shipped now.
> * **Validation:** regen the HTML mocks first (with side-by-side variants for the open inter-step visual), discuss, then unit tests on the pure `turn-grouping` functions (the scenarios as cases); manual testing in the real app by the user.

---

## Overview

- The chat progress view currently decides which assistant messages render at the top level using a window-containment rule in `selectFinalMessages` (`turn-grouping.ts`): a text-only message is top-level if it falls outside every step's active window, plus the single last text-only message if its containing step is done or settled.
- That rule produces three concrete defects at end-of-turn, confirmed by tracing the code and mocked in `attachments/end-of-turn-scenarios.html`:
  1. **Dropped messages** — when multiple text-only messages land inside a done/settled step's window, only the last is promoted; earlier ones disappear from the top level (only visible by expanding the step).
  2. **Contextless unclosed steps** — a step left open when the agent goes idle shows a bare title plus a "settled" tag (internal jargon), with no summary and no narration (narration is actively suppressed for settled steps).
  3. **Narration→reply visual jump** — a final message inside an unclosed step renders as italic shimmering narration while the agent is active, then jumps to a plain top-level block once the agent goes idle.
- Agents are unreliable about *when* they close steps; prompting does not fix this in practice. So the rendering layer must produce a clean result for every open/closed/idle/active combination.
- The fix replaces window-containment with a **backward-scan reply rule** plus **position-aware placement** of top-level text (above / interrupting / below the timeline). A `closed` event is a hard stop for the scan, which makes the ideal agent behavior simple to state and steer toward: close the last step, then write the reply.
- Steering (claude.md) reinforces that ideal but is explicitly not relied upon. A `tk close` escape-hatch flag is designed but deferred.

## Expected behavior

Scenario references map to `attachments/end-of-turn-scenarios.html`.

- **Reply detection is a backward scan.** From the last event of a turn, walk backward gathering text-only assistant messages. Stop at the first `closed` task event or any tool activity (a tool call or tool result). The gathered run (in chronological order) is the turn's top-level reply.
  - Close-then-speak (S2): the post-close message is the reply → rendered below the timeline.
  - Speak-then-close (S3): the message precedes the close, so the scan stops at the close and the message is **not** promoted — it stays as in-step content. This is the accepted trade-off; the deferred flag is the escape hatch.
  - Speak / more tools / speak (S7): only the trailing message (after the last tool result) is the reply; the earlier message remains in-step narration (it was followed by tool activity), not dropped from the timeline's expandable body.
  - Speak, close, speak again (S4): the scan stops at the close, so only the post-close message is promoted. The pre-close message stays in-step.
- **Top-level text is placed by position, never hidden:**
  - **Leading** text emitted before the first step is created renders *above* the entire timeline as plain prose.
  - **Inter-step** text emitted after a step closes and before the next step starts *interrupts* the timeline at that chronological point (exact visual chosen during mock review).
  - **Trailing** reply renders *below* the timeline as plain prose.
- **Unclosed steps when the agent is idle (S5, S6, S8):**
  - No "settled" tag.
  - Static partial-ring icon (unchanged glyph) signals "was in progress, stopped."
  - If a promoted reply exists below, the step shows no caption (the reply carries the meaning). If some in-step narration survives and there's no better context, it may render as a static, italic, muted caption (no shimmer).
- **Active (streaming) turns are unchanged in spirit (S9–S12):**
  - In-step narration still shows as the live shimmering caption while the agent works.
  - Once the agent goes idle, the trailing reply is promoted below the timeline; because the backward-scan result is positional rather than tied to `is_settled` narration promotion, the active→idle transition no longer produces the jarring narration→reply jump (the final message is consistently a top-level block).
- **UngroupedWorkBlock (no steps declared):** same backward-scan reply rule (no close events, so "after the last tool result") with leading text above the ungrouped node and the trailing reply below it. Inter-step interruption does not apply (there are no steps).
- **Steering:** claude.md tells agents the ideal is to close the final step before the wrap-up reply, and briefly explains why (so the reply lands after the last close and is promoted). Never defaults to relying on an escape hatch.

## Changes

Relative to the existing system, without implementation detail:

- **`turn-grouping.ts` — replace the reply-selection model:**
  - Replace `selectFinalMessages`'s window-containment logic with the backward-scan rule (stop at first `closed` event or tool activity), returning the trailing text-only run.
  - Add a notion of message *position* (leading / inter-step / trailing) so the renderer can place top-level text above, within, or below the timeline rather than always below.
  - Adjust `attributeNarration` so in-step narration is the text-only messages that were followed by tool activity in the same step (mid-work narration), decoupled from the `is_settled` suppression that currently blanks unclosed-idle steps.
  - Revisit `stepActiveInWindow` / window-end computation only as needed to support positional classification; keep the serial-step invariant and trailing-tool-result pull-in.
- **`ProgressBlock.ts` — positional rendering:**
  - Render leading top-level messages above the timeline, inter-step messages interleaved at their position (thread-interrupting treatment, final visual TBD), and trailing reply below.
  - Remove the "settled" carryover-style tag for unclosed-idle steps; keep the partial-ring icon; render captions only when there is genuine context, using a static (non-shimmer) muted style for settled captions.
- **`UngroupedWorkBlock.ts` — consistency:**
  - Adopt the same backward-scan reply rule and leading-above / trailing-below placement around the single ungrouped node.
- **`style.css` — progress-view styles:**
  - Add styles for the inter-step thread-interruption block and the leading-above message position.
  - Add a static (non-animated) variant of the settled-step caption; ensure the shimmer is reserved for genuinely active narration.
- **claude.md — steering:**
  - Add concise guidance: close the final step before writing the user-facing wrap-up reply, with a one-line rationale tying it to how the progress view promotes replies. Frame as best-effort, not a hard requirement.
- **Mocks — `attachments/end-of-turn-scenarios.html`:**
  - Regenerate to cover the full scenario matrix under the new rules, and present **side-by-side variants** for the open inter-step visual question (continuous-thread inset card vs. thread marker/glyph vs. broken-thread full-width block).
- **Tests — `turn-grouping.test.ts`:**
  - Add cases encoding the scenario matrix (close/no-close × idle/active × leading/inter-step/trailing × single/multiple messages), asserting reply detection and positional classification on the pure functions.
- **Deferred (not built now):** a `tk close` flag (e.g. signaling "promote my already-given wrap-up message") in `vendor/tk`, plus a close-time hook/nudge. Documented as future work; add only if manual testing shows speak-then-close is common.

## Open questions

- **Inter-step visual (the one deliberately-open item):** which treatment reads most intuitively as "the agent paused mid-plan to speak" — a continuous thread with an inset aside card, a thread marker/glyph with text beside it, or a clean broken-thread full-width block? To be decided after reviewing side-by-side mock variants.
- **Settled-caption fallback:** when an unclosed-idle step has in-step narration but no promoted reply, do we surface that narration as a static caption, or prefer a fully bare node? Lean toward showing it, but confirm against the regenerated mocks.
- **Escape-hatch trigger:** what real-world frequency of speak-then-close (S3) would justify building the deferred `tk close` flag? Decide from manual testing observations.
