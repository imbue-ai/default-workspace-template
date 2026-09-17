/**
 * The shared look of a node on the turn timeline -- the row that a tk step
 * (ProgressBlock) and an agent handoff (handoff-node) both render as. The two
 * draw different bullets and titles but the same row, so the recipe lives here
 * rather than being copied into each.
 *
 * The `pv-*` class names are bare markers (the e2e suite and the scroll
 * verifier locate rows by them); the utilities beside them carry the look.
 */

import m from "mithril";

/**
 * How a node's title reads. Finished work greys out; everything else stays at
 * full strength, and the one the agent is on right now shimmers in step with
 * its narration so the current task pulses as a unit.
 */
export type TimelineTone = "done" | "current" | "upcoming";

/**
 * The live bullet: the shared ring spinner in the accent, sized to the 16px
 * bullet grid the drawn status badges use (see statusDoneIcon and friends).
 * `[--spinner-width:1.5px]` runs it a half-step lighter than the spinner's own
 * default so it carries the same weight as the still badges beside it.
 */
export function timelineSpinnerBullet(): m.Vnode {
  return m(
    "span",
    {
      class:
        "pv-icon pv-icon--active inline-flex h-4 w-4 shrink-0 items-center justify-center text-accent " +
        "[--spinner-width:1.5px]",
    },
    m("span.spinner.spinner--sm.spinner--current"),
  );
}

/**
 * The row itself, minus the status markers each call site adds. The right-hand
 * inset is the agent's gutter (see --width-agent-gutter): task rows stop short
 * of the column edge exactly as the agent's prose does, so the timeline and the
 * replies around it share one right margin.
 */
export const TIMELINE_NODE_CLASS = "relative flex max-w-[calc(100%-var(--width-agent-gutter))] items-start gap-3.5";

/** The bullet gutter. Its opaque chat background masks the thread behind it. */
export const TIMELINE_BULLET_CLASS = "pv-tl-bullet relative z-(--z-content) w-4 shrink-0 bg-chat py-px";

/** Everything right of the bullet: title, caption, and the expanded body. */
export const TIMELINE_BODY_CLASS = "pv-tl-body min-w-0 flex-1";

/** text-[13.5px]: a half-step below body, off the type scale on purpose -- at
 *  body size the titles competed with the prose they summarise. */
const TITLE_BASE =
  "pv-tl-title inline-flex cursor-pointer items-center appearance-none border-0 bg-transparent p-0 text-left " +
  "text-[13.5px] leading-[1.4] font-medium disabled:cursor-default";

/** The title's tone. `pv-tl-title--current` is the hook for the shimmer, which
 *  is a keyframe + background-clip machine and so lives in style.css. */
export function timelineTitleClass(tone: TimelineTone): string {
  if (tone === "done") return `${TITLE_BASE} text-faint`;
  if (tone === "current") return `${TITLE_BASE} pv-tl-title--current text-primary`;
  return `${TITLE_BASE} text-primary`;
}

/** The expand chevron beside the title. text-[18px]: icon glyph, sized
 *  independently of the text scale (and deliberately not text-lg, whose
 *  line-height would reflow the row). */
export const TIMELINE_CHEVRON_CLASS =
  "pv-chev ml-1.5 inline-block text-[18px] font-normal transition-transform duration-(--dur-base) ease-[ease]";

/** The chevron's open/closed half, appended to {@link TIMELINE_CHEVRON_CLASS}. */
export function timelineChevronStateClass(isExpanded: boolean): string {
  return isExpanded ? "pv-chev--open rotate-90 text-primary" : "text-secondary";
}
