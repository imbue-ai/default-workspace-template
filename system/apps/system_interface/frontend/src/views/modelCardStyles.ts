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

/** Show ten rows before the list starts scrolling. Fewer and a long catalog reads as a
 *  keyhole -- pi's was showing three; many more and the flyout is a wall. Derived rather than
 *  guessed so it stays true if the row height changes. */
const FLYOUT_ROW_HEIGHT = 29;
const FLYOUT_VISIBLE_ROWS = 10;
/** Rows, plus the search field standing under them, plus the shell's own padding. */
export const FLYOUT_MAX_HEIGHT = FLYOUT_ROW_HEIGHT * FLYOUT_VISIBLE_ROWS + 42;

// --- the composer trigger ------------------------------------------------------------------
export const TRIGGER =
  "flex h-[30px] items-center gap-1.5 rounded-lg px-2 type-helper whitespace-nowrap " +
  "text-tertiary transition-colors hover:bg-fill-hover hover:text-secondary cursor-pointer";
/** The separators between the chip's three parts, a step quieter than the values. */
export const TRIGGER_DOT = "text-tertiary/60";

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
export const SLIDER_WRAP = "relative flex h-4 w-32 items-center";
/** Inset by half the thumb's width on each side.
 *
 *  A range input's thumb CENTER travels from `thumbWidth/2` to `width - thumbWidth/2`, never to
 *  the track's actual edges -- so ticks spread across the full width put the first and last one
 *  6px outside anywhere the ball can reach, and the ball sits off its own mark at both ends.
 *  Spanning the thumb's real travel instead makes every tick a position the ball lands on. */
export const SLIDER_TICKS =
  "pointer-events-none absolute left-1.5 right-1.5 top-1/2 flex -translate-y-1/2 justify-between";
/** Taller than the 12px knob and dark enough to read through it: these are the delimiters
 *  that say where the levels ARE, so they have to survive the thumb passing over them. */
/** Hairline, but dark and taller than the 12px thumb, so it reads through the ball. */
export const SLIDER_TICK = "h-[15px] w-px bg-primary/70";
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
/** `model-flyout-scroll` gives it a visible slim scrollbar; without one nothing says the
 *  list continues past the edge. */
export const FLYOUT_SCROLL = "model-flyout-scroll min-h-0 flex-1 overflow-y-auto";
const FLYOUT_ROW_SHAPE =
  "flex w-full items-center gap-1.5 rounded-md px-1.5 text-left text-[13px] leading-[29px] transition-colors";
/** `pr-14` reserves the right edge for the tick and the removal control beside it, so the row's
 *  text never reflows when the bin appears. */
const FLYOUT_ROW_BASE = `${FLYOUT_ROW_SHAPE} pr-14`;
/** An ACCOUNT row reserves one slot more, for the rename pencil. Its own base rather than a
 *  wider shared one: model rows carry no pencil, and widening what they share would truncate
 *  every model name by 24px to make room for a control that is never drawn on them. */
const ACCOUNT_ROW_BASE = `${FLYOUT_ROW_SHAPE} pr-20`;
export const FLYOUT_ROW = `${FLYOUT_ROW_BASE} text-primary hover:bg-fill-hover cursor-pointer`;
export const FLYOUT_ROW_SELECTED = `${FLYOUT_ROW_BASE} bg-fill-active text-primary cursor-pointer`;
export const ACCOUNT_ROW = `${ACCOUNT_ROW_BASE} text-primary hover:bg-fill-hover cursor-pointer`;
export const ACCOUNT_ROW_SELECTED = `${ACCOUNT_ROW_BASE} bg-fill-active text-primary cursor-pointer`;
/** A provider on a harness this chat cannot switch to. DIVERGES from the mockup's inert
 *  `text-tertiary cursor-default` only by keeping the hover highlight, so the row still feels
 *  live enough that the user waits for the tooltip that explains it. */
export const ACCOUNT_ROW_LOCKED = `${ACCOUNT_ROW_BASE} text-tertiary hover:bg-fill-hover cursor-default`;
export const FLYOUT_ROW_NAME = "truncate";
export const FLYOUT_ROW_SUB = "type-helper text-tertiary";
/** Pinned to the row's right edge and never moved. A SIBLING of the row button rather than a
 *  child, so the removal control can sit to its LEFT without either one having to give way.
 *
 *  The mockup slides this aside on hover to let the trash take the edge. It does not here: the
 *  tick says which provider this chat is running on, and that fact does not change because the
 *  pointer passed over the row. */
export const FLYOUT_CHECK = "ml-auto shrink-0 text-accent";
/** The same tick on a row that also carries a removal control: pinned to the row's right edge
 *  as a SIBLING of the button, so the bin can sit to its LEFT without either giving way.
 *
 *  The mockup slides the tick aside on hover to let the bin take the edge. It does not here: the
 *  tick says which provider this chat runs on, and that does not change because the pointer
 *  passed over the row. */
export const FLYOUT_CHECK_PINNED = "pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-accent";
export const FLYOUT_EMPTY = "type-helper text-tertiary px-1.5 py-2";
export const FLYOUT_ADD =
  "flex w-full items-center gap-1.5 rounded-md px-1.5 text-left text-[13px] leading-[29px] " +
  "text-secondary transition-colors hover:bg-fill-hover cursor-pointer";

/** The sign-out control: a SIBLING of the row button (buttons cannot nest), floated over the
 *  row's reserved right padding. `right-7` puts it LEFT of the tick rather than on top of it,
 *  so the tick never has to move out of its way. */
export const ROW_TRASH =
  "absolute right-8 top-1/2 hidden h-5 w-5 -translate-y-1/2 cursor-pointer items-center " +
  "justify-center rounded text-tertiary transition-colors hover:text-important group-hover/conn:inline-flex";
/** Armed: stays visible whether or not the row is hovered, and says what it will do. Same right
 *  edge as the bin it replaces, so arming it does not shift anything. */
/** Armed, it is wider than the bin it replaces, so it carries the row's own background: it may
 *  overhang the provider name rather than forcing every row to reserve space for a word that is
 *  almost never shown. */
/** The rename control: a SIBLING of the row button like the bin, one slot further in at
 *  `right-14`, so the pencil, the bin and the tick each keep their own lane and none of the
 *  three ever moves. Hidden while the bin is armed -- "Remove?" is wide enough to sit under
 *  the pencil, and a row asking whether to delete itself should not also offer to rename. */
export const ROW_PENCIL =
  "absolute right-14 top-1/2 hidden h-5 w-5 -translate-y-1/2 cursor-pointer items-center " +
  "justify-center rounded text-tertiary transition-colors hover:text-primary group-hover/conn:inline-flex";
/** The row mid-rename: the field takes the whole row, since every control is hidden while it
 *  is up. Same metrics as the row it replaces, so nothing shifts on the way in or out. */
export const ROW_RENAME_INPUT =
  "w-full rounded-md border border-subtle bg-surface-primary px-1.5 text-[13px] leading-[27px] " +
  "text-primary outline-none";

/* No tooltip classes live here on purpose.
 *
 * The mockup floats its own `bottom-full` chip inside the popover. That cannot work in this
 * app: both the card and the flyout are `overflow-hidden`, so the chip is cut off mid-sentence,
 * and both are `z-[120]` fixed boxes, so each one is its own stacking context and a chip inside
 * the card can never rise above the flyout beside it. Both were visible in the shipped build.
 *
 * `hoverTooltip.ts` already solves exactly this -- a single fixed bubble on <body>, with
 * hover-intent, viewport clamping and flip -- and it exists BECAUSE this app clips CSS bubbles.
 * Rows spread `hoverTooltipAttrs(...)` instead.
 */
