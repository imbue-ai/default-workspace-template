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
import { railNodeAttrs } from "./agent-rail";

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
 * default so it carries the same weight as the static badges beside it.
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

/** The row itself, minus the status markers each call site adds. */
export const TIMELINE_NODE_CLASS = "relative flex items-start gap-3.5";

/**
 * The bullet gutter: exactly one title line tall, centring the bullet in it, so
 * the bullet and the title agree by construction rather than by two line boxes
 * happening to work out the same. Dead centre, with no optical nudge: the
 * stretch's line meets the bullet from above and below, and stops the same
 * distance short of it on both sides.
 *
 * The height must track {@link TITLE_BASE}'s `leading-[1.4]` at
 * `--font-size-body`; both are written out because Tailwind scans class strings
 * literally and would miss a shared constant.
 */
const TIMELINE_BULLET_CLASS = "pv-tl-bullet flex h-[calc(var(--font-size-body)*1.4)] w-4 shrink-0 items-center";

/** A node's bullet in its gutter, marked as a stop on the stretch's line. */
export function renderTimelineBullet(icon: m.Children): m.Vnode {
  return m("div", { class: TIMELINE_BULLET_CLASS }, m("span", { class: "flex", ...railNodeAttrs("step") }, icon));
}

/** A run of timeline nodes: in from the column so the bullets' centres sit on the stretch's line, with the 6px
 *  beyond the stretch's spacing that sets the run 18px from its neighbours, as its nodes are from each other. */
export const TIMELINE_STEPS_CLASS = "my-1.5 pl-[14.5px]";

/** Everything right of the bullet: title, caption, and the expanded body. */
export const TIMELINE_BODY_CLASS = "pv-tl-body min-w-0 flex-1";

/**
 * A task title is body text: it is the row's subject, not chrome. What sets it
 * apart from the prose around it is the medium weight and the tone, not a size
 * of its own -- the captions under it drop to helper instead.
 *
 * `flex w-fit`, not `inline-flex`: an inline-level button is baseline-aligned
 * inside the BODY's line box, which puts the block's leading above it.
 * Block-level starts the title at the body's top edge, where the bullet is;
 * `w-fit` keeps it shrink-wrapped to its text so the clickable area is the
 * title and not the whole row.
 */
const TITLE_BASE =
  "pv-tl-title flex w-fit cursor-pointer items-center appearance-none border-0 bg-transparent p-0 text-left " +
  "text-(length:--font-size-body) leading-[1.4] font-medium disabled:cursor-default";

/** The title's tone. `pv-tl-title--current` is the hook for the shimmer, which
 *  is a keyframe + background-clip machine and so lives in style.css. */
export function timelineTitleClass(tone: TimelineTone): string {
  if (tone === "done") return `${TITLE_BASE} text-faint`;
  if (tone === "current") return `${TITLE_BASE} pv-tl-title--current text-primary`;
  return `${TITLE_BASE} text-primary`;
}

/** The title's words, apart from the chevron beside them. The shimmer is clipped to the text of the element that
 *  wears it, so it goes on the words alone: worn by the whole title it would also paint the chevron's glyph, unturned,
 *  under the chevron's own turned one. */
export function renderTimelineTitleText(text: string): m.Vnode {
  return m("span", { class: "pv-tl-title-text" }, text);
}

/** The expand chevron beside the title. text-[18px]: icon glyph, sized
 *  independently of the text scale (and deliberately not text-lg, whose
 *  line-height would reflow the row). `leading-none` for the same reason one
 *  step further: at 18px the glyph's own line box is taller than the title's,
 *  which would make an expandable title sit lower than a plain one. */
export const TIMELINE_CHEVRON_CLASS =
  "pv-chev ml-1.5 inline-block text-[18px] leading-none font-normal transition-transform duration-(--dur-base) " +
  "ease-[ease]";

/** The chevron's open/closed half, appended to {@link TIMELINE_CHEVRON_CLASS}. */
export function timelineChevronStateClass(isExpanded: boolean): string {
  return isExpanded ? "pv-chev--open rotate-90 text-primary" : "text-secondary";
}
