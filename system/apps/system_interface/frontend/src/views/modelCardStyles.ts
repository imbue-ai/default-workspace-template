/**
 * The combo card's class strings, copied from the mockup.
 *
 * `prototypes/minds-harness` in imbue-ai/mind-sketches is the design source, and the rule is
 * that a surface it defines is ported verbatim rather than re-derived -- see the addendum in
 * `findings/07-plan-v3.md`. Kept in one file, annotated with where each came from, so the two
 * can still be diffed after the fact.
 *
 * Everything here resolves: the colour tokens, the `type-*` ramp and `shadow-overlay` are all
 * registered in `style.css`. That is not incidental -- Tailwind v4 emits NOTHING for an unknown
 * utility, so an unregistered class is a silent no-op with no build error to catch it.
 */

// --- the card ------------------------------------------------------------------------------
// ModelBar.tsx's combo card container.
export const CARD =
  "fixed z-[110] flex w-[300px] flex-col rounded-xl border border-subtle bg-surface-primary p-1 shadow-overlay";

/** One row. Label left, value right -- the card reads as a compact spec sheet. */
export const ROW =
  "flex w-full items-center gap-2 rounded-md px-1.5 text-left text-[13px] leading-[29px] " +
  "text-primary transition-colors hover:bg-fill-hover cursor-pointer";
/** A row with no menu behind it (effort, fast) -- same metrics, no hover affordance. */
export const ROW_STATIC = "flex items-center gap-2 px-1.5 leading-[29px]";
/** Labels sit at the values' own size, one colour step back. */
export const ROW_LABEL = "text-[13px] text-secondary";
export const ROW_VALUE = "ml-auto flex min-w-0 items-center gap-1.5";
export const ROW_VALUE_STATIC = "ml-auto flex items-center gap-2";
export const ROW_TEXT = "truncate";
/** The harness, beside the provider it runs. */
export const ROW_SUBTEXT = "type-helper text-tertiary";
export const ROW_CHEVRON = "shrink-0 text-tertiary";
/** Full-bleed divider; the negative margin cancels the card's p-1. The provider is the
 *  card's "who", everything below is the "how". */
export const DIVIDER = "-mx-1 my-1 border-t border-subtle";

// --- the effort slider ---------------------------------------------------------------------
export const EFFORT_VALUE = "text-[12px] text-primary";
export const SLIDER =
  "h-[3px] w-24 cursor-pointer appearance-none rounded-full " +
  "[&::-moz-range-thumb]:h-3 [&::-moz-range-thumb]:w-3 [&::-moz-range-thumb]:rounded-full " +
  "[&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:bg-white " +
  "[&::-moz-range-thumb]:shadow-[0_0_0_1px_rgba(0,0,0,0.15),0_1px_2px_rgba(0,0,0,0.25)] " +
  "[&::-webkit-slider-thumb]:h-3 [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:appearance-none " +
  "[&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-white " +
  "[&::-webkit-slider-thumb]:shadow-[0_0_0_1px_rgba(0,0,0,0.15),0_1px_2px_rgba(0,0,0,0.25)]";

// --- the fast row --------------------------------------------------------------------------
export const FAST_LABEL = "flex items-center gap-1.5";
export const FAST_LABEL_OFF = "opacity-60";

// --- the flyout (model list / provider list) -----------------------------------------------
export const FLYOUT =
  "fixed z-[120] flex w-[280px] flex-col overflow-hidden rounded-xl border border-subtle " +
  "bg-surface-primary p-1 shadow-overlay";
export const FLYOUT_SCROLL = "min-h-0 flex-1 overflow-y-auto";
export const FLYOUT_ROW =
  "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] " +
  "text-primary transition-colors hover:bg-fill-hover cursor-pointer";
/** A provider on a harness this chat cannot switch to. Not `disabled`: a disabled button
 *  suppresses :hover, and the hover is what explains why. */
export const FLYOUT_ROW_LOCKED =
  "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] " +
  "text-tertiary opacity-60 cursor-pointer";
export const FLYOUT_ROW_NAME = "truncate";
export const FLYOUT_ROW_SUB = "type-helper text-tertiary";
export const FLYOUT_CHECK = "ml-auto shrink-0 text-accent";
export const FLYOUT_SECTION = "type-section text-tertiary px-2 pt-2 pb-1";
export const FLYOUT_EMPTY = "type-helper text-tertiary px-2 py-2";
/** The growth affordance at the foot of the provider list. */
export const FLYOUT_ADD = FLYOUT_ROW + " text-accent";

// --- the composer trigger ------------------------------------------------------------------
/** What sits in the composer and opens the card. Reuses the existing selector-trigger
 *  vocabulary so it lines up with the other composer controls. */
export const TRIGGER = "model-selector-trigger";
export const TRIGGER_LABEL = "model-selector-label";
export const TRIGGER_SUB = "model-selector-sub";
