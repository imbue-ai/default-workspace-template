/**
 * The combo card's class strings, copied from the mockup.
 *
 * `prototypes/minds-harness` in imbue-ai/mind-sketches is the design source (`ModelBar.tsx`'s
 * `ComboCardView` / `ProvidersView` / `ModelFlyout`, and `Switch.tsx`). The rule is that a
 * surface the mockup defines is ported verbatim rather than re-derived, so the two can still
 * be diffed. Divergences are marked DIVERGES and say why.
 *
 * Everything here resolves: the colour tokens, the `type-*` ramp and `shadow-overlay` are all
 * registered in `style.css`. That is not incidental -- Tailwind v4 emits NOTHING for an unknown
 * utility, so an unregistered class is a silent no-op with no build error to catch it. The
 * mockup scopes its accent to #2f6b4f through an inline `--c-accent`; this app's `--color-accent`
 * IS that green already, so `bg-accent` / `text-accent` are used bare.
 */

/** DIVERGES: the mockup's card is 300 and its slider `w-24`. Both were too tight to aim the
 *  effort thumb at, so the card carries 40px more and spends it on the slider. */
export const CARD_WIDTH = 340;
export const FLYOUT_WIDTH = 300;
/** macOS submenu geometry: the flyout tucks 4px UNDER the card's right edge. */
export const FLYOUT_OVERLAP = 4;

// --- the composer trigger ------------------------------------------------------------------
export const TRIGGER =
  "flex h-[30px] items-center gap-1 rounded-lg px-2 type-helper whitespace-nowrap " +
  "text-tertiary transition-colors hover:bg-fill-hover hover:text-secondary cursor-pointer";

// --- the card ------------------------------------------------------------------------------
export const CARD =
  "fixed z-[120] overflow-hidden rounded-xl border border-subtle bg-surface-primary p-1 shadow-overlay";
export const CARD_INNER = "flex flex-col";

/** A row that drills into a flyout. `w-full` plus the row's own padding IS the click target:
 *  the mockup's rows are clickable edge to edge. */
export const ROW =
  "flex w-full items-center gap-2 rounded-md px-1.5 text-left text-[13px] leading-[29px] " +
  "text-primary transition-colors hover:bg-fill-hover cursor-pointer";
/** DIVERGES: a row with nothing to drill into -- a read-only harness's model. The mockup has
 *  no such state (every model there is switchable). Keeps the hover highlight, because a row
 *  that does not react at all reads as broken rather than as fixed. */
export const ROW_INERT =
  "flex w-full items-center gap-2 rounded-md px-1.5 text-left text-[13px] leading-[29px] " +
  "text-primary transition-colors hover:bg-fill-hover cursor-default";
export const ROW_STATIC = "flex items-center gap-2 px-1.5 leading-[29px]";
/** Labels sit at the values' own size, one colour step back -- the card reads as a spec sheet. */
export const ROW_LABEL = "text-[13px] text-secondary";
export const ROW_VALUE = "ml-auto flex min-w-0 items-center gap-1.5";
export const ROW_VALUE_STATIC = "ml-auto flex items-center gap-2";
export const ROW_TEXT = "truncate";
export const ROW_SUBTEXT = "type-helper text-tertiary";
export const ROW_CHEVRON = "shrink-0 text-tertiary";
/** Full-bleed divider; the negative margin cancels the card's p-1. The provider is the card's
 *  "who", everything below is the "how". */
export const DIVIDER = "-mx-1 my-1 border-t border-subtle";
/** Marks a row group as a tooltip host. */
export const ROW_WRAP = "group/conn relative";

// --- the effort slider ---------------------------------------------------------------------
export const EFFORT_VALUE = "text-[12px] text-primary";
/** DIVERGES: wraps the track so tick marks can sit behind it -- the mockup's bare slider gives
 *  no clue where the levels are, which is exactly what makes it feel like guesswork. */
export const SLIDER_WRAP = "relative flex h-3 w-32 items-center";
export const SLIDER_TICKS =
  "pointer-events-none absolute inset-x-0 top-1/2 flex -translate-y-1/2 justify-between";
export const SLIDER_TICK = "h-[7px] w-px bg-strong";
export const SLIDER =
  "relative h-[3px] w-full cursor-pointer appearance-none rounded-full " +
  "[&::-moz-range-thumb]:h-3 [&::-moz-range-thumb]:w-3 [&::-moz-range-thumb]:rounded-full " +
  "[&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:bg-white " +
  "[&::-moz-range-thumb]:shadow-[0_0_0_1px_rgba(0,0,0,0.15),0_1px_2px_rgba(0,0,0,0.25)] " +
  "[&::-webkit-slider-thumb]:h-3 [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:appearance-none " +
  "[&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-white " +
  "[&::-webkit-slider-thumb]:shadow-[0_0_0_1px_rgba(0,0,0,0.15),0_1px_2px_rgba(0,0,0,0.25)]";

// --- the fast switch (mockup Switch.tsx, verbatim) -----------------------------------------
export const SWITCH =
  "relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors cursor-pointer " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
  "disabled:cursor-not-allowed disabled:opacity-50";
export const SWITCH_ON = "bg-accent";
export const SWITCH_OFF = "bg-fill-active";
export const SWITCH_KNOB =
  "inline-flex h-5 w-5 items-center justify-center rounded-full bg-white shadow-sm transition-transform";
export const SWITCH_KNOB_ON = "translate-x-[22px]";
export const SWITCH_KNOB_OFF = "translate-x-[2px]";
export const SWITCH_CHECK = "text-accent";

// --- the flyouts ---------------------------------------------------------------------------
export const FLYOUT =
  "fixed z-[120] flex flex-col overflow-hidden rounded-xl border border-subtle " +
  "bg-surface-primary p-1 shadow-overlay";
export const FLYOUT_SCROLL = "min-h-0 flex-1 overflow-y-auto";
/** `pr-7` reserves the slot the trash occupies on hover, so the row's text never reflows. */
const FLYOUT_ROW_BASE =
  "flex w-full items-center gap-1.5 rounded-md px-1.5 pr-7 text-left text-[13px] leading-[29px] " +
  "transition-colors";
export const FLYOUT_ROW = `${FLYOUT_ROW_BASE} text-primary hover:bg-fill-hover cursor-pointer`;
export const FLYOUT_ROW_SELECTED = `${FLYOUT_ROW_BASE} bg-fill-active text-primary cursor-pointer`;
/** A provider on a harness this chat cannot switch to. DIVERGES from the mockup's inert
 *  `text-tertiary cursor-default` only by keeping the hover highlight, so the row still feels
 *  live enough that the user waits for the tooltip that explains it. */
export const FLYOUT_ROW_LOCKED =
  `${FLYOUT_ROW_BASE} text-tertiary hover:bg-fill-hover cursor-default`;
export const FLYOUT_ROW_NAME = "truncate";
export const FLYOUT_ROW_SUB = "type-helper text-tertiary";
/** Sits at the right edge, translated over the reserved slot; on hover it slides back so the
 *  trash takes the edge -- it only moves when actually displaced, never just fading out. */
export const FLYOUT_CHECK =
  "ml-auto shrink-0 translate-x-[18px] text-accent transition-transform group-hover/conn:translate-x-0";
export const FLYOUT_EMPTY = "type-helper text-tertiary px-1.5 py-2";
export const FLYOUT_ADD =
  "flex w-full items-center gap-1.5 rounded-md px-1.5 text-left text-[13px] leading-[29px] " +
  "text-secondary transition-colors hover:bg-fill-hover cursor-pointer";

/** The sign-out trash: a SIBLING of the row button (buttons cannot nest), floated over the
 *  row's reserved right padding, appearing on row hover. */
export const ROW_TRASH =
  "absolute right-1.5 top-1/2 hidden h-5 w-5 -translate-y-1/2 cursor-pointer items-center " +
  "justify-center rounded text-tertiary transition-colors hover:text-important group-hover/conn:inline-flex";
/** Armed: stays visible whether or not the row is hovered, and says what it will do. */
export const ROW_TRASH_ARMED =
  "absolute right-1 top-1/2 inline-flex h-5 -translate-y-1/2 cursor-pointer items-center " +
  "justify-center rounded px-1 type-helper text-important hover:bg-fill-hover";

/** The mockup's TOOLTIP_ABOVE_CONN: our own black chip rather than the OS title bubble,
 *  floated above the row it explains, with the mockup's hover delay ramp. */
export const TOOLTIP_ABOVE_ROW =
  "pointer-events-none absolute bottom-full left-1/2 z-[130] mb-0.5 -translate-x-1/2 " +
  "whitespace-nowrap rounded-sm bg-surface-inverse px-1.5 py-0.5 text-xs text-inverse-primary " +
  "opacity-0 transition-opacity duration-0 delay-0 group-hover/conn:opacity-100 " +
  "group-hover/conn:delay-150 group-hover/conn:duration-100";
/** The same chip, but wrapping: the read-only hint is a sentence, not a phrase. */
export const TOOLTIP_ABOVE_ROW_WRAP = TOOLTIP_ABOVE_ROW.replace("whitespace-nowrap", "w-56 text-center");
