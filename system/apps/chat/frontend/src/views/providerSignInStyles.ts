/**
 * The provider chooser's own class strings: the layout and the few looks that
 * are genuinely particular to this flow.
 *
 * Everything with a shared recipe behind it is NOT here -- buttons come from
 * `components/Button`, text fields from `components/Input`, the panel frame from
 * `components/Modal`, the key picker from `components/menu`, colour from the
 * semantic utility layer and size from the `type-*` roles. What remains below is
 * the residue: the four panel sizes, the row shapes the flow lists things in, and
 * the verdict screen.
 *
 * A note for anyone extending this: nothing here is exempt from the design
 * system. An earlier pass carried this flow's styling over wholesale from a
 * design prototype, which left it speaking a private vocabulary -- its own
 * button, its own input, its own scrim, its own stacking layers -- beside an app
 * that already had all four. If a look you need exists in `components/`, use it;
 * if it does not, add it there rather than here.
 */

import { cardClass } from "@imbue/workspace-ui/src/components/card";

/** The workspace's modal-card chrome (see MODAL_CARD_CLASS in components/Modal.ts) minus its
 *  fixed width and padding -- the panel carries the width and pads its own regions. */
export const MODAL =
  "overflow-hidden rounded-lg border border-default bg-body-surface-raised shadow-overlay " +
  "animate-[modal-card-in_var(--dur-slow)_cubic-bezier(0.16,1,0.3,1)]";
/** The panel's frame.
 *
 *  ONE width for every screen, so drilling in and backing out never resizes the dialog
 *  sideways -- 460px, near the shared modal card's 420 (MODAL_CARD_CLASS), and wearing its
 *  `max-w-[90vw]` so a narrow viewport shrinks the panel rather than hanging it off the edge.
 *  Only HEIGHT is per-screen now; see `panelBody`.
 *
 *  Height is CONTENT-driven, not set: the overlay centers rather than stretches, the body has
 *  no height of its own, and the footer sits outside the scroll region -- so the panel is
 *  exactly header + content + footer until the content passes its screen's cap, at which point
 *  the body scrolls and the panel stops growing. */
export const PANEL = "flex w-[460px] max-w-[90vw] flex-col";

/** The body's shared part. Its ceiling comes from `panelBody`, because how much room a screen
 *  deserves is a fact about that screen. */
const BODY = "overflow-y-auto overscroll-contain px-4 pb-3 pt-1";

/** Each screen's body ceiling. Height is the axis that still varies: a confirmation is a
 *  sentence, a method list is a column of rows, and the lane list is the tallest thing the
 *  flow ever shows -- a method list has no business being as tall as the catalog.
 *
 *  Each is `min(px, vh)` so the tallest screen still fits a laptop; the entry screen's FLOOR is
 *  clamped the same way, or on a short viewport a floor above the ceiling would push the panel
 *  off the bottom of the screen. */
export function panelBody(screen: "status" | "menu" | "form" | "chooser"): string {
  return {
    status: `${BODY} max-h-[min(320px,60vh)]`,
    menu: `${BODY} max-h-[min(420px,62vh)]`,
    form: `${BODY} max-h-[min(480px,62vh)]`,
    // The one screen the flow always returns to, so it also keeps a floor: a panel that shrinks
    // under the pointer on the way back reads as something having gone wrong.
    chooser: `${BODY} max-h-[min(560px,62vh)] min-h-[min(380px,45vh)]`,
  }[screen];
}

export const HEADER = "flex items-center gap-1.5 px-4 pb-3 pt-3";
export const TITLE = "m-0 type-heading text-body-content";

export const ROW_STACK = "flex flex-col gap-2";

/** A lane row: the whole provider, as something to walk into. The shared interactive card,
 *  because the row IS the affordance -- there is no other control on it. */
export const CHOOSER_ROW = cardClass({
  interactive: true,
  extra:
    "flex w-full items-center gap-2 p-3 text-left " +
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
});
/** Reserved even when a lane has no mark, so every label lines up. */
export const CHOOSER_ROW_MARK = "flex w-11 shrink-0 items-center justify-center";
export const CHOOSER_ROW_TEXT = "min-w-0 flex-1";
export const CHOOSER_ROW_NAME = "block type-body font-semibold text-body-content";
export const CHOOSER_ROW_BODY = "mt-0.5 block text-(length:--font-size-body-dense) leading-snug text-body-content";
export const CHEVRON = "shrink-0 text-body-content-tertiary";

/** One sign-in method inside a lane. The chooser row's shape at one step less weight, since
 *  by here the provider is settled and only the method is in question. */
export const OPTION_ROW = `${CHOOSER_ROW} justify-between`;
export const OPTION_ROW_NAME = "type-body font-medium text-body-content";
export const OPTION_ROW_DESC = "mt-0.5 type-helper text-body-content-secondary";

export const SECTION_LABEL = "mb-3.5 type-section text-body-content-secondary";
/** The break above a section that is an aside to the one before it, with equal air on both
 *  sides so the rule reads as a gap rather than as the lid on what follows.
 *
 *  DASHED, where the card's own section rules are solid: a solid line would split the screen
 *  into two halves of equal standing, and the fallbacks under it are not that -- they are the
 *  footnote to a method the screen has already picked out. */
export const SECTION_BREAK = "mt-6 border-t border-dashed border-default pt-6";
export const LEAD = "mb-4 type-body leading-relaxed text-body-content";
/** A link inside the lead. Where to GET the key, said in the sentence that explains the screen
 *  rather than promoted to a numbered step -- "go to a website if you have not already" is not
 *  half of a two-part procedure. */
export const LEAD_LINK = "text-accent-surface underline underline-offset-2 hover:text-accent-surface-hover";
/** Something to do on the provider's side before the sign-in can work. Under the lead, in full
 *  colour rather than a hint's faint, because skipping it fails the sign-in. */
export const NOTE =
  "-mt-2 mb-4 type-helper leading-snug text-body-content " +
  "[&_a]:text-accent-surface [&_a]:underline [&_a]:underline-offset-2 [&_a]:hover:text-accent-surface-hover";

/** The right-hand end of the header: the harness line and the close button, as one group.
 *
 *  ONE `ml-auto`, on the pair. Giving each of them their own makes them fight: the first
 *  pushes the label right, the second pushes the button further still, and the label is left
 *  stranded in the gap between them. */
export const HEADER_END = "ml-auto flex shrink-0 items-center gap-4";
/** Which harness this sign-in will run on. Provider -> harness is fixed, so it is a fact the
 *  screen states rather than a control -- and helper size, not body, because it sits beside
 *  the heading as an aside rather than reading as a second title. */
export const RUNS_ON = "inline-flex shrink-0 items-baseline gap-1.5 type-helper";
export const RUNS_ON_PREFIX = "text-body-content-tertiary";
/** The harness is the fact worth reading here, so it carries the weight and the full contrast;
 *  "Runs on" is just the label in front of it. */
export const RUNS_ON_NAME = "font-semibold text-body-content";

/** A step block, and the desaturation put on the one you are past. */
export const STEP = "mb-4 transition-all duration-(--dur-slow)";
export const STEP_LAST = "transition-all duration-(--dur-slow)";
export const STEP_DIMMED = "grayscale opacity-60";
export const STEP_LABEL =
  "mb-2 flex items-center gap-2 text-(length:--font-size-body-dense) font-medium text-body-content";
export const STEP_NUM =
  "inline-flex h-[18px] w-[18px] items-center justify-center rounded-full bg-accent-surface " +
  "type-helper font-semibold text-accent-content";

export const FIELD_ROW = "flex items-center gap-2";

/** One right-aligned action under the body. */
export const FOOTER = "px-4 pb-3 pt-3";
export const FOOTER_ROW = "flex justify-end";
/** The browser wait's two ways forward, side by side. */
export const RELAY_ACTIONS = "mt-4 flex flex-wrap gap-2";

/** Secondary prose under a field or step. */
export const HINT = "mt-1.5 type-helper leading-snug text-body-content-tertiary";
/** A hint that says what went wrong: the same line in the danger colour. */
export const HINT_ERROR = "mt-1.5 type-helper leading-snug text-danger-surface";
export const HINT_ACTION = "underline underline-offset-2 hover:text-body-content cursor-pointer";

/** Shown when a clipboard write was refused, so the value is still reachable by hand. */
export const RAW_VALUE =
  "mt-2 select-all break-all rounded-md border border-default bg-body-surface-secondary p-2 font-mono " +
  "type-helper leading-snug text-body-content";

/** The device flow's one-time code. Label weight in mono with a little tracking: legible
 *  enough to read aloud and retype, and level with the copy button beside it. */
export const CODE =
  "flex-1 rounded-md bg-body-surface-secondary p-2 text-center font-mono type-label tracking-[0.12em] text-body-content select-all";

/** verifying / success / error, one shape for all three.
 *
 *  The verdict reads as a label over its detail line rather than as a second title: the modal
 *  header already names the screen, so the verdict only has to state the outcome. */
export const STATUS = "flex flex-col items-center px-2 py-8 text-center";
const STATUS_DISC = "mb-3 flex h-12 w-12 items-center justify-center rounded-full";
export const STATUS_DISC_PENDING = `${STATUS_DISC} text-accent-surface`;
export const STATUS_DISC_SUCCESS = `${STATUS_DISC} bg-accent-subtle-surface text-accent-surface`;
export const STATUS_DISC_ERROR = `${STATUS_DISC} bg-danger-subtle-surface text-danger-surface`;
export const STATUS_TITLE = "type-title-sm text-body-content";
export const STATUS_DETAIL = "mt-1.5 max-w-[340px] type-body leading-snug text-body-content-secondary";
/** The provider's own mark, under the success check -- so "signed in" names WHICH. */
export const STATUS_MARK = "mt-3 flex items-center justify-center gap-2 type-helper text-body-content-tertiary";

/** A signed-in account: the shared card without its affordances, because the row is a listed
 *  fact rather than somewhere to navigate. Its actions carry the interactivity. */
const ACCOUNT_ROW_LAYOUT = "flex w-full items-center gap-2 p-3 text-left";
export const ACCOUNT_ROW = cardClass({ extra: ACCOUNT_ROW_LAYOUT });
/** The same row when the chooser was opened to pick an account: the whole row picks it, as a
 *  lane row walks into its lane, while its own actions stay on top and keep their clicks. Its
 *  edge deepens under the pointer as an interactive card's does, but only while the pick is
 *  enabled, so it is not the interactive card itself. */
export const ACCOUNT_ROW_PICKABLE =
  `${ACCOUNT_ROW} relative transition-[border-color] duration-(--dur-base) ` +
  "has-[.account-pick:enabled]:hover:border-default-hover " +
  "has-[.account-pick:focus-visible]:outline-2 has-[.account-pick:focus-visible]:outline-offset-2 " +
  "has-[.account-pick:focus-visible]:outline-focus";
/** The pick target. Its `after` box stretches over the whole row, so a click anywhere on the
 *  row that is not one of the actions lands here. */
export const ACCOUNT_PICK =
  "account-pick flex min-w-0 flex-1 cursor-pointer items-center gap-2 text-left outline-none " +
  "after:absolute after:inset-0 after:rounded-2xl after:content-[''] disabled:cursor-default";
/** Raises an action above the pick target's stretched box. */
export const ACCOUNT_ACTION = "relative";
export const ACCOUNT_NEUTRAL_NOTE = "shrink-0 type-helper text-body-content-secondary";
export const ACCOUNT_FAILING_NOTE = "shrink-0 type-helper text-danger-surface";
