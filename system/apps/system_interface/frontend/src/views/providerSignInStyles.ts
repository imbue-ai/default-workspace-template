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

import { menuCardClass } from "./components/menu";

/** The workspace's modal-card chrome (see MODAL_CARD_CLASS in components/Modal.ts) minus its
 *  fixed width and padding -- the panel sizes per screen and pads its own regions. */
export const MODAL =
  "overflow-hidden rounded-lg border border-default bg-surface shadow-overlay " +
  "animate-[modal-card-in_var(--dur-slow)_cubic-bezier(0.16,1,0.3,1)]";
/** The panel's frame. Both dimensions come from `panelSize` below, per screen.
 *
 *  Height is CONTENT-driven, not set: the overlay centers rather than stretches, the body has
 *  no height of its own, and the footer sits outside the scroll region -- so the panel is
 *  exactly header + content + footer until the content passes its screen's cap, at which point
 *  the body scrolls and the panel stops growing. */
export const PANEL = "flex flex-col transition-[width] duration-(--dur-base) ease-out";

/** The body's shared part. Its ceiling comes from `panelSize`, because how much room a screen
 *  deserves is a fact about that screen. */
const BODY = "overflow-y-auto overscroll-contain px-6 pb-5 pt-1";

/** Each screen's width and its body's ceiling.
 *
 *  Four steps on one scale, so drilling in never feels like a new dialog: a confirmation is a
 *  sentence, a method list is a column of rows, a form is a column of fields, and the lane list
 *  is the widest and tallest thing the flow ever shows. The ceilings are per-screen for the
 *  same reason the widths are -- a method list has no business being as tall as the catalog.
 *
 *  Each is `min(px, vh)` so the tallest screen still fits a laptop; the entry screen's FLOOR is
 *  clamped the same way, or on a short viewport a floor above the ceiling would push the panel
 *  off the bottom of the screen. */
export function panelSize(screen: "status" | "menu" | "form" | "chooser"): { width: string; body: string } {
  return {
    status: { width: "w-[440px]", body: `${BODY} max-h-[min(320px,60vh)]` },
    menu: { width: "w-[600px]", body: `${BODY} max-h-[min(420px,62vh)]` },
    form: { width: "w-[640px]", body: `${BODY} max-h-[min(480px,62vh)]` },
    // The one screen the flow always returns to, so it also keeps a floor: a panel that shrinks
    // under the pointer on the way back reads as something having gone wrong.
    chooser: { width: "w-[690px]", body: `${BODY} max-h-[min(560px,62vh)] min-h-[min(380px,45vh)]` },
  }[screen];
}

export const HEADER = "flex items-center gap-1.5 px-6 pb-4 pt-5";
export const TITLE = "m-0 type-heading text-primary";

export const ROW_STACK = "flex flex-col gap-2";

/** A lane row: the whole provider, as something to walk into. Raised off the panel and
 *  outlined on hover, because the row IS the affordance -- there is no other control on it. */
export const CHOOSER_ROW =
  "flex w-full items-center gap-2 rounded-lg border border-default bg-surface p-3 text-left " +
  "shadow-raised transition-[border-color,box-shadow] duration-(--dur-base) hover:border-accent " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent cursor-pointer";
/** Reserved even when a lane has no mark, so every label lines up. */
export const CHOOSER_ROW_MARK = "flex w-11 shrink-0 items-center justify-center";
export const CHOOSER_ROW_TEXT = "min-w-0 flex-1";
export const CHOOSER_ROW_NAME = "block type-body font-semibold text-primary";
export const CHOOSER_ROW_BODY = "mt-0.5 block text-(length:--font-size-row) leading-snug text-primary";
export const CHEVRON = "shrink-0 text-faint";

/** One sign-in method inside a lane. The chooser row's shape at one step less weight, since
 *  by here the provider is settled and only the method is in question. */
export const OPTION_ROW = `${CHOOSER_ROW} justify-between`;
export const OPTION_ROW_NAME = "type-body font-medium text-primary";
export const OPTION_ROW_DESC = "mt-0.5 type-helper text-secondary";

export const SECTION_LABEL = "mb-3.5 type-section text-secondary";
export const LEAD = "mb-4 type-body leading-relaxed text-primary";
/** A link inside the lead. Where to GET the key, said in the sentence that explains the screen
 *  rather than promoted to a numbered step -- "go to a website if you have not already" is not
 *  half of a two-part procedure. */
export const LEAD_LINK = "text-accent underline underline-offset-2 hover:text-accent-hover";

/** The right-hand end of the header: the harness line and the close button, as one group.
 *
 *  ONE `ml-auto`, on the pair. Giving each of them their own makes them fight: the first
 *  pushes the label right, the second pushes the button further still, and the label is left
 *  stranded in the gap between them. */
export const HEADER_END = "ml-auto flex shrink-0 items-center gap-4";
/** Which harness this sign-in will run on. Provider -> harness is fixed, so it is a fact the
 *  screen states rather than a control. */
export const RUNS_ON = "inline-flex shrink-0 items-baseline gap-1.5 type-body";
export const RUNS_ON_PREFIX = "text-faint";
/** The harness is the fact worth reading here, so it carries the weight and the full contrast;
 *  "Runs on" is just the label in front of it. */
export const RUNS_ON_NAME = "font-semibold text-primary";

/** A step block, and the desaturation put on the one you are past. */
export const STEP = "mb-4 transition-all duration-(--dur-slow)";
export const STEP_LAST = "transition-all duration-(--dur-slow)";
export const STEP_DIMMED = "grayscale opacity-60";
export const STEP_LABEL = "mb-2 flex items-center gap-2 text-(length:--font-size-row) font-medium text-primary";
export const STEP_NUM =
  "inline-flex h-[18px] w-[18px] items-center justify-center rounded-full bg-accent " +
  "type-helper font-semibold text-on-accent";

export const FIELD_ROW = "flex items-center gap-2";

/** One right-aligned action under the body. */
export const FOOTER = "px-6 pb-5 pt-4";
export const FOOTER_ROW = "flex justify-end";

/** Secondary prose under a field or step. */
export const HINT = "mt-1.5 type-helper leading-snug text-faint";
export const HINT_ACTION = "underline underline-offset-2 hover:text-primary cursor-pointer";

/** Shown when a clipboard write was refused, so the value is still reachable by hand. */
export const RAW_VALUE =
  "mt-2 select-all break-all rounded-md border border-default bg-sidebar p-2 font-mono " +
  "type-helper leading-snug text-primary";

/** The device flow's one-time code: the one place a value is meant to be read aloud off the
 *  screen and typed somewhere else, so it is set far above any type role. */
export const CODE =
  "flex-1 rounded-md bg-sidebar p-3 text-center font-mono text-[22px] tracking-[0.12em] " + "text-primary select-all";

/** verifying / success / error, one shape for all three.
 *
 *  The verdict is the whole screen, so it is sized like one: a bigger disc, a heading at full
 *  strength rather than a step back, and a detail line in the body colour. */
export const STATUS = "flex flex-col items-center px-2 py-8 text-center";
const STATUS_DISC = "mb-4 flex h-16 w-16 items-center justify-center rounded-full";
export const STATUS_DISC_PENDING = `${STATUS_DISC} text-accent`;
export const STATUS_DISC_SUCCESS = `${STATUS_DISC} bg-accent-light text-accent`;
export const STATUS_DISC_ERROR = `${STATUS_DISC} bg-danger-surface text-danger`;
export const STATUS_TITLE = "type-heading-lg text-primary";
export const STATUS_DETAIL = "mt-1.5 max-w-[340px] type-body leading-snug text-secondary";
/** The provider's own mark, under the success check -- so "signed in" names WHICH. */
export const STATUS_MARK = "mt-3 flex items-center justify-center gap-2 type-helper text-faint";

/** A signed-in account: the chooser row's frame without its affordances, because the row is a
 *  listed fact rather than somewhere to navigate. Its actions carry the interactivity. */
export const ACCOUNT_ROW =
  "flex w-full items-center gap-2 rounded-lg border border-default bg-surface p-3 text-left shadow-raised";

// --- The API-key screen's provider dropdown ------------------------------------------------

/** The field-shaped trigger. Sized and framed like the key input beside it (see
 *  `inputClass`), since the two read as one form. */
export const PICKER_TRIGGER =
  "flex w-full items-center justify-between gap-2 rounded-md border border-default bg-surface " +
  "px-3 py-2 text-left type-body transition-[border-color] duration-(--dur-base) " +
  "hover:border-accent focus-visible:outline-2 focus-visible:outline-offset-2 " +
  "focus-visible:outline-accent cursor-pointer";
export const PICKER_TRIGGER_VALUE = "flex min-w-0 items-baseline gap-2";
export const PICKER_TRIGGER_NAME = "truncate text-primary";
export const PICKER_TRIGGER_ENV = "shrink-0 font-mono type-helper text-faint";
export const PICKER_TRIGGER_EMPTY = "text-faint";
export const PICKER_CARET = "shrink-0 text-faint transition-transform";
export const PICKER_CARET_OPEN = "rotate-180";

/** Swallows the outside click that closes the menu, so it never reaches the modal beneath.
 *  Both live inside the overlay's stacking context, so the dropdown layer only has to clear
 *  the panel; the menu paints over its own backdrop by DOM order. */
export const PICKER_BACKDROP = "fixed inset-0 z-(--z-dropdown) cursor-default";
/** Pinned under the trigger, wearing the shared floating-menu chrome. The panel is
 *  overflow-hidden, so an in-panel popover would be clipped -- hence the portal. */
export const PICKER_MENU = menuCardClass("fixed max-h-[280px] overflow-y-auto overscroll-contain");
/** The shared menu row shape (menuRowClass), minus its hover: the active row keeps its steady
 *  accent fill, so only the idle variant hovers. */
export const PICKER_OPTION =
  "flex h-8 w-full cursor-pointer items-center justify-between gap-3 px-3 text-left " +
  "focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent";
export const PICKER_OPTION_IDLE = "hover:bg-fill-hover";
export const PICKER_OPTION_ACTIVE = "bg-accent-light";
export const PICKER_OPTION_NAME = "truncate type-body";
export const PICKER_OPTION_NAME_ACTIVE = "truncate type-body text-accent";
