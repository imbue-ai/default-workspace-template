/**
 * The composer combo card's own class strings: what is left after the shared recipes.
 *
 * The card's frame, rows and divider are the workspace's floating-menu chrome
 * (`components/menu`), and its flyouts are the same chrome again -- so this menu
 * matches the tab menu and the rail's row menus. What lives here is the part no
 * other surface has: the effort slider, the fast-mode switch, and the row
 * geometry that reserves a lane for each of a row's trailing controls.
 *
 * Every colour, size and elevation below comes from the semantic utility layer,
 * the `type-*` roles and the shadow tokens. That is not incidental -- Tailwind v4
 * emits NOTHING for an unknown utility, so a name this app does not define is a
 * silent no-op with no build error to catch it (`providerSignInStyles.test.ts`
 * guards both this module and the chooser's against exactly that).
 */

import { menuCardClass, menuDividerClass, menuRowClass } from "@imbue/workspace-ui/src/components/menu";

/** Wide enough to aim the effort thumb at: the slider needs the room, and the rows read as a
 *  spec sheet rather than a cramped list. */
export const CARD_WIDTH = 340;
export const FLYOUT_WIDTH = 300;
/** macOS submenu geometry: the flyout tucks 4px UNDER the card's right edge. */
export const FLYOUT_OVERLAP = 4;

/** Show ten rows before the list starts scrolling. Fewer and a long catalog reads as a
 *  keyhole -- pi's was showing three; many more and the flyout is a wall. Derived rather than
 *  guessed so it stays true if the row height changes. */
const FLYOUT_ROW_HEIGHT = 32;
const FLYOUT_VISIBLE_ROWS = 10;
/** The distance from a flyout's outer top edge to the top of its first row: `menuCardClass`
 *  gives it a 1px border AND `py-1`, and both sit above the row. The flyout is placed by that
 *  first ROW rather than by the box around it, so the placement backs off by exactly this --
 *  miss the border and every flyout lands a pixel low. */
const FLYOUT_BORDER = 1;
const FLYOUT_INNER_PADDING = 4;
export const FLYOUT_PADDING = FLYOUT_BORDER + FLYOUT_INNER_PADDING;
/** `SEARCH_INPUT_EXTRA`'s `h-8` plus `SEARCH_WRAP`'s `mb-1.5` under it. */
const SEARCH_FIELD_HEIGHT = 32 + 6;

/** How tall a flyout of `rowCount` rows wants to be, measured the way the browser measures a
 *  bordered box: both borders and both paddings, which is what `2 * FLYOUT_PADDING` is.
 *
 *  The placement slides the box up when this will not fit below the row it belongs to, so the
 *  arithmetic has to match what the DOM actually lays out -- it is the same row height and the
 *  same chrome the cap below is built from, and both move together if either changes. */
export function flyoutContentHeight(rowCount: number, hasSearchField: boolean): number {
  return 2 * FLYOUT_PADDING + rowCount * FLYOUT_ROW_HEIGHT + (hasSearchField ? SEARCH_FIELD_HEIGHT : 0);
}

/** Ten rows, plus the search field standing under them, plus the box's own padding. */
export const FLYOUT_MAX_HEIGHT = flyoutContentHeight(FLYOUT_VISIBLE_ROWS, true);

// --- the composer trigger ------------------------------------------------------------------
export const TRIGGER =
  "flex h-[30px] items-center gap-1 rounded-lg px-2 type-helper whitespace-nowrap " +
  "text-faint transition-colors hover:bg-fill-hover hover:text-secondary cursor-pointer";
/** The separators between the chip's three parts, a step quieter than the values. */
export const TRIGGER_DOT = "text-faint/60";

// --- the card ------------------------------------------------------------------------------
/** The invisible sheet under an open card. It takes every hover and every press that is not on
 *  the card itself, so while the card is up nothing behind it lights up, wakes a tooltip, or
 *  receives a click. Same `--z-dropdown` layer as the card, which stays on top of it by
 *  rendering after it as a sibling -- the shape the project rail's menus already use.
 *  `model-card-scrim` is a bare marker for tests and devtools, with no CSS attached. */
export const SCRIM = "model-card-scrim fixed inset-0 z-(--z-dropdown)";

/** The workspace's shared menu chrome; `fixed` and the width are the caller's. `overflow-hidden`
 *  keeps a full-bleed row highlight inside the rounded corners. */
export const CARD = menuCardClass("fixed overflow-hidden text-(length:--font-size-row)");
export const CARD_INNER = "flex flex-col";

/** A row that drills into a flyout. The shared row IS the click target: full width, full-bleed
 *  highlight, edge to edge. */
export const ROW = menuRowClass({ extra: "text-primary" });
/** A row with nothing to drill into -- a read-only harness's model. Keeps the hover highlight,
 *  because a row that does not react at all reads as broken rather than as fixed. */
export const ROW_INERT = menuRowClass({ inert: true, extra: "text-primary" });
export const ROW_STATIC = "flex h-8 items-center gap-2 px-3";
/** Labels sit at the values' own size, one colour step back -- the card reads as a spec sheet. */
export const ROW_LABEL = "text-secondary";
export const ROW_VALUE = "ml-auto flex min-w-0 items-center gap-1.5";
export const ROW_VALUE_STATIC = "ml-auto flex items-center gap-2";
export const ROW_TEXT = "truncate";
export const ROW_SUBTEXT = "type-helper text-faint";
export const ROW_CHEVRON = "shrink-0 text-faint";
/** The provider is the card's "who", everything below is the "how". */
export const DIVIDER = menuDividerClass();
/** Marks a row group as a tooltip host. */
export const ROW_WRAP = "group/conn relative";

// --- the effort slider ---------------------------------------------------------------------
export const EFFORT_VALUE = "type-helper text-primary";
/** Wraps the track so the level dots can be positioned over it -- a bare slider gives no clue
 *  where the levels are, which is exactly what makes it feel like guesswork. */
export const SLIDER_WRAP = "relative flex h-4 w-32 items-center";
/** Inset by half the thumb's width on each side.
 *
 *  A range input's thumb CENTER travels from `thumbWidth/2` to `width - thumbWidth/2`, never to
 *  the track's actual edges -- so ticks spread across the full width put the first and last one
 *  6px outside anywhere the ball can reach, and the ball sits off its own mark at both ends.
 *  Spanning the thumb's real travel instead makes every tick a position the ball lands on. */
export const SLIDER_TICKS = "pointer-events-none absolute inset-x-1.5 top-1/2 z-10";
/** A dot at each level, sitting ON the track (hence the container's `z-10`) rather than behind
 *  it: a 2px mark behind a 6px track is not a faint dot, it is no dot at all.
 *
 *  Placed by its CENTRE -- `left` at the level's own fraction of the thumb's travel, pulled back
 *  half its own width -- rather than by laying the dots out with `justify-between`, which spaces
 *  their BOXES and so leaves the first and last centres a pixel inside the stops they mark. At
 *  2px wide that pixel is half the dot.
 *
 *  The dot under the thumb is not drawn at all (see `effortRow`): the ball is the mark for the
 *  level it is parked on, and a dot showing through it reads as a second, smaller mark. */
export const SLIDER_TICK =
  "absolute top-0 h-[2px] w-[2px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary/75";
export const SLIDER =
  "relative h-[6px] w-full cursor-pointer appearance-none rounded-full " +
  "[&::-moz-range-thumb]:h-3 [&::-moz-range-thumb]:w-3 [&::-moz-range-thumb]:rounded-full " +
  "[&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:bg-surface " +
  "[&::-moz-range-thumb]:shadow-[0_0_0_1px_rgba(0,0,0,0.15),0_1px_2px_rgba(0,0,0,0.25)] " +
  "[&::-webkit-slider-thumb]:h-3 [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:appearance-none " +
  "[&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-surface " +
  "[&::-webkit-slider-thumb]:shadow-[0_0_0_1px_rgba(0,0,0,0.15),0_1px_2px_rgba(0,0,0,0.25)]";

// --- the fast switch -----------------------------------------------------------------------
/** The switch comes in two sizes, and a size is a TRACK PLUS ITS THROW -- never one without
 *  the other.
 *
 *  That is not a style preference, it is the bug this table exists to prevent. The
 *  terminal-view toggle once scaled the one switch down with its own CSS `transform`, against a
 *  knob whose offset already came from a Tailwind `translate-x-[...]` utility. The utility won,
 *  and a 22px throw inside a shrunken track put the knob outside it. Asking for a size by name
 *  is the only way to get one, so the two can no longer disagree.
 *
 *  Each row is arithmetic, not taste: the knob is the track's height less 2px of inset top and
 *  bottom, and the on-position is `width - inset - knob`, which leaves the knob the same 2px
 *  from either end. `md` is the combo card's fast-mode row; `sm` is the composer's under-bar,
 *  where the switch sits beside a line of helper text and has to read as its equal, not as the
 *  loudest thing down there. */
const SWITCH_SIZES = {
  md: { track: "h-6 w-11", knob: "h-5 w-5", on: "translate-x-[22px]" },
  sm: { track: "h-4 w-[30px]", knob: "h-3 w-3", on: "translate-x-[16px]" },
} as const;

export type SwitchSize = keyof typeof SWITCH_SIZES;

/** The track. Colour is the caller's -- `SWITCH_ON` / `SWITCH_OFF` below. */
export function switchClass(size: SwitchSize): string {
  return (
    `relative inline-flex ${SWITCH_SIZES[size].track} shrink-0 items-center rounded-full transition-colors ` +
    "cursor-pointer focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
    "disabled:cursor-not-allowed disabled:opacity-50"
  );
}

/** The knob, already at the position `on` puts it in. */
export function switchKnobClass(size: SwitchSize, on: boolean): string {
  const chosen = SWITCH_SIZES[size];
  return (
    `inline-flex ${chosen.knob} items-center justify-center rounded-full bg-surface shadow-raised ` +
    `transition-transform ${on ? chosen.on : SWITCH_KNOB_OFF}`
  );
}

export const SWITCH_ON = "bg-accent";
export const SWITCH_OFF = "bg-fill-active";
/** The same 2px at rest whatever the size, so it is not in the table. */
const SWITCH_KNOB_OFF = "translate-x-[2px]";
export const SWITCH_CHECK = "text-accent";

// --- the flyouts ---------------------------------------------------------------------------
/** Same shared chrome as the card. The flex column caps the scroll region beneath the pinned
 *  search field. */
export const FLYOUT = menuCardClass("fixed flex flex-col overflow-hidden text-(length:--font-size-row)");
/** The search field at the head of the list. The wrapper positions the magnifier over the
 *  field's own left padding; the field itself is the shared `inputClass`, so its frame, focus
 *  ring and placeholder match every other text field in the workspace. The margin is BELOW it,
 *  separating it from the list it filters -- above it the flyout's own padding is the gap. */
export const SEARCH_WRAP = "relative mx-1.5 mb-1.5";
export const SEARCH_ICON = "pointer-events-none absolute left-2.5 top-1/2 z-(--z-content) -translate-y-1/2 text-faint";
/** Room for the magnifier, and the dense-chrome row size the rest of the flyout sits at. */
export const SEARCH_INPUT_EXTRA = "h-8 py-0 pl-8 text-(length:--font-size-row)";

/** `model-flyout-scroll` gives it a visible slim scrollbar; without one nothing says the
 *  list continues past the edge. */
export const FLYOUT_SCROLL = "model-flyout-scroll min-h-0 flex-1 overflow-y-auto";
/** The shared row shape, minus its hover/cursor: the selected and locked variants below need
 *  to say those themselves, and a `hover:` from the base would override a selected row's
 *  steady fill. Keep in step with `menuRowClass` -- `mx-1 w-[calc(100%-0.5rem)] rounded px-2`
 *  is its inset highlight slab, and the reasoning for each part lives there. */
const FLYOUT_ROW_SHAPE =
  "flex h-8 items-center gap-1.5 mx-1 w-[calc(100%-0.5rem)] rounded px-2 text-left " +
  "focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent";
/** `pr-14` reserves the right edge for the tick and the removal control beside it, so the row's
 *  text never reflows when the bin appears. Those controls are positioned against the row's
 *  WRAPPER rather than the row, so they stay put while the highlight insets around them. */
const FLYOUT_ROW_BASE = `${FLYOUT_ROW_SHAPE} pr-14`;
/** An ACCOUNT row reserves two slots more, for the rename pencil and the default star. Its
 *  own base rather than a wider shared one: model rows carry neither, and widening what they
 *  share would truncate every model name to make room for controls that are never drawn on
 *  them. */
const ACCOUNT_ROW_BASE = `${FLYOUT_ROW_SHAPE} pr-26`;
export const FLYOUT_ROW = `${FLYOUT_ROW_BASE} text-primary hover:bg-fill-hover cursor-pointer`;
export const FLYOUT_ROW_SELECTED = `${FLYOUT_ROW_BASE} bg-fill-active text-primary cursor-pointer`;
export const ACCOUNT_ROW = `${ACCOUNT_ROW_BASE} text-primary hover:bg-fill-hover cursor-pointer`;
export const ACCOUNT_ROW_SELECTED = `${ACCOUNT_ROW_BASE} bg-fill-active text-primary cursor-pointer`;
export const FLYOUT_ROW_NAME = "truncate";
export const FLYOUT_ROW_SUB = "type-helper text-faint";
/** Pinned to the row's right edge and never moved. A SIBLING of the row button rather than a
 *  child, so the removal control can sit to its LEFT without either one having to give way.
 *
 *  It does not slide aside on hover: the tick says which provider this chat is running on, and
 *  that fact does not change because the pointer passed over the row. */
export const FLYOUT_CHECK = "ml-auto shrink-0 text-accent";
/** The same tick on a row that also carries a removal control: pinned to the row's right edge
 *  as a SIBLING of the button, so the bin can sit to its LEFT without either giving way. It
 *  does not slide aside on hover, for the same reason as above. */
export const FLYOUT_CHECK_PINNED = "pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-accent";
export const FLYOUT_EMPTY = "type-helper text-faint px-3 py-2";
export const FLYOUT_ADD = menuRowClass({ extra: "text-secondary" });

/** The sign-out control: a SIBLING of the row button (buttons cannot nest), floated over the
 *  row's reserved right padding. `right-7` puts it LEFT of the tick rather than on top of it,
 *  so the tick never has to move out of its way. */
export const ROW_TRASH =
  "absolute right-9 top-1/2 hidden h-5 w-5 -translate-y-1/2 cursor-pointer items-center " +
  "justify-center rounded text-faint transition-colors hover:text-danger group-hover/conn:inline-flex";
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
  "absolute right-15 top-1/2 hidden h-5 w-5 -translate-y-1/2 cursor-pointer items-center " +
  "justify-center rounded text-faint transition-colors hover:text-primary group-hover/conn:inline-flex";
/** The row mid-rename: the field takes the whole row, since every control is hidden while it
 *  is up. The wrapper insets the bordered field from the card's full-bleed edges; the field
 *  keeps the row's height, so nothing shifts on the way in or out. */
/** The default toggle: the outermost lane at `right-21`, shown on hover like the pencil and
 *  the bin. The pinned account's star stays visible (and filled) whether or not the row is
 *  hovered: it is a fact about which account a new chat opens on, not a control that only
 *  matters while the pointer is there. */
export const ROW_STAR =
  "absolute right-21 top-1/2 hidden h-5 w-5 -translate-y-1/2 cursor-pointer items-center " +
  "justify-center rounded text-faint transition-colors hover:text-primary group-hover/conn:inline-flex";
export const ROW_STAR_PINNED =
  "absolute right-21 top-1/2 inline-flex h-5 w-5 -translate-y-1/2 cursor-pointer items-center " +
  "justify-center rounded text-accent transition-colors hover:text-primary";
export const ROW_RENAME_WRAP = `${ROW_WRAP} px-1.5`;
export const ROW_RENAME_INPUT =
  "h-8 w-full rounded-md border border-default bg-surface px-2 text-primary outline-none";

/* No tooltip classes live here on purpose.
 *
 * A CSS bubble inside the popover cannot work: both the card and the flyout are
 * `overflow-hidden`, so it is cut off mid-sentence, and both are fixed boxes on the dropdown
 * layer, so each is its own stacking context and a chip inside the card can never rise above
 * the flyout beside it.
 *
 * `hoverTooltip.ts` already solves exactly this -- a single fixed bubble on <body>, with
 * hover-intent, viewport clamping and flip -- and it exists BECAUSE this app clips CSS bubbles.
 * Rows spread `hoverTooltipAttrs(...)` instead.
 */
