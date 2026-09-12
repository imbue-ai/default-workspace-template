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

/** The card and its flyouts are one width. The card was 40px wider, for the effort slider's
 *  travel -- which turned out not to depend on it: the slider is a fixed 128px, so the extra
 *  width went to the row values, and a flyout narrower than the card it slides out of reads as
 *  a mistake at the seam where they meet. */
export const CARD_WIDTH = 300;
export const FLYOUT_WIDTH = 300;
/** macOS submenu geometry: the flyout tucks UNDER the card's edge rather than sitting beside
 *  it. 5px, because that is where the opening ROW ends -- the card's 1px border plus the 4px
 *  its row highlight is inset by -- and the flyout meeting that highlight is what reads as the
 *  row continuing sideways. Tucking under the card's edge alone left a 1px seam. */
export const FLYOUT_OVERLAP = 5;

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
 *  bottom, the on-position is `width - inset - knob`, which leaves the knob the same 2px from
 *  either end, and the tick is the fraction of the knob that leaves it a rim.
 *
 *  Both switches in the chat are `sm` today -- the composer's under-bar one, which sits beside
 *  a line of helper text and has to read as its equal, and the card's fast-mode row, which sits
 *  in a column of values and should not be the loudest thing in it. `md` is the full-size step,
 *  which nothing asks for at the moment; it stays because a size that is not in this table is a
 *  size that can disagree with itself, which is the whole reason the table exists. */
const SWITCH_SIZES = {
  md: { track: "h-6 w-11", knob: "h-5 w-5", on: "translate-x-[22px]", check: 12 },
  sm: { track: "h-4 w-[30px]", knob: "h-3 w-3", on: "translate-x-[16px]", check: 8 },
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

/** The tick inside the knob, at the size that knob can hold: 12px in a 20px knob, 8px in a
 *  12px one, which is the same rim either way. Asked for by name with the track and the throw,
 *  for the same reason they are -- a tick sized by hand would be the one part of a switch free
 *  to disagree with the size it is drawn in. */
export function switchCheckSize(size: SwitchSize): number {
  return SWITCH_SIZES[size].check;
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
/** An ACCOUNT row reserves its right edge for the star, the rename pencil and the bin, which are
 *  positioned against the row's WRAPPER rather than the row, so they stay put while the
 *  highlight insets around them, and the provider name never reflows when they appear. 80px is
 *  the outermost of the three at its far edge (`right-15` plus its own 20px), so the reserve
 *  says exactly what is in the row and not a pixel more. Its own base rather than a wider
 *  shared one: a MODEL row carries none of them -- only the tick, as a child -- and reserving
 *  this width on model rows too would truncate every model name to make room for controls that
 *  are never drawn on them. */
const ACCOUNT_ROW_BASE = `${FLYOUT_ROW_SHAPE} pr-20`;
export const FLYOUT_ROW = `${FLYOUT_ROW_SHAPE} text-primary hover:bg-fill-hover cursor-pointer`;
export const FLYOUT_ROW_SELECTED = `${FLYOUT_ROW_SHAPE} bg-fill-active text-primary cursor-pointer`;
/** Lit from the WRAPPER, not the row button. The star, the pencil and the bin are SIBLINGS of
 *  that button, so a pointer on one of them is not on the row by CSS's reckoning -- and the
 *  row would drop its highlight at exactly the moment the pointer arrived at the controls that
 *  highlight had just revealed. */
export const ACCOUNT_ROW = `${ACCOUNT_ROW_BASE} text-primary group-hover/conn:bg-fill-hover cursor-pointer`;
export const ACCOUNT_ROW_SELECTED = `${ACCOUNT_ROW_BASE} bg-fill-active text-primary cursor-pointer`;
export const FLYOUT_ROW_NAME = "truncate";
/** A MODEL name, truncated from the FRONT. `openrouter/qwen/qwen-2.5-72b-instruct` is a path
 *  whose head repeats down the whole list and whose tail is the only part that tells one row
 *  from another, so dropping the head is the only truncation that leaves anything to read by.
 *
 *  `direction: rtl` is what moves the ellipsis: it puts the line's END at the LEFT, which is
 *  the side the box overflows, and a name too long to fit hangs its beginning off there. The
 *  name goes in a `<bdi>` so it stays one isolated left-to-right run -- without the isolation a
 *  name ending in a neutral character, `Sonnet 4.5 (thinking)` say, would have its bracket
 *  reordered to the far left. `text-align: left` is for names that DO fit, which an rtl box
 *  would otherwise push against its right edge. */
export const MODEL_NAME = "truncate text-left [direction:rtl]";
export const FLYOUT_ROW_SUB = "type-helper text-faint";
/** The MODEL row's tick: the last thing in the row, pushed out by `ml-auto` and so landing on
 *  the row's own `px-2` padding edge -- which is where the account rows' pinned tick sits too,
 *  13px in from the flyout's edge, so the two lists' ticks share one line.
 *
 *  That alignment is why the model row reserves no right padding of its own. It used to carry
 *  the account row's `pr-14`, for a bin and a pencil a model row never draws, and `ml-auto`
 *  duly stopped at the inside edge of that reserve: a tick floating 56px short of the row's
 *  end, which is what it looked like. A flex tick reads the row's padding, so the padding has
 *  to be the truth about what else is in the row. */
export const FLYOUT_CHECK = "ml-auto inline-flex h-5 w-5 shrink-0 items-center justify-center text-accent";
/* Both ticks stand in the same 20x20 box the row's control buttons do, centred in it.
 *
 * A bare 13px glyph and a 13px glyph centred in a 20px button do not share a centre line even
 * when their boxes end on the same pixel: the button's padding puts its glyph 3.5px further in.
 * So the tick sat half a glyph-width off the star above it, which is exactly what a column of
 * marks makes visible. Giving the tick the button's box is what puts every glyph in these
 * lists -- tick, star, pencil, bin, in either flyout -- on one centre line. */
/** The same tick on a row that also carries controls: pinned to the row's right edge as a
 *  SIBLING of the button, since buttons cannot nest.
 *
 *  It stands down while the row is hovered. The three controls want the row's end -- a control
 *  group that stops short of the edge to leave a mark room reads as misaligned -- and the tick
 *  is the one thing there that can afford to go: what it says is still said by the row's own
 *  selected fill, and it comes straight back when the pointer leaves. */
export const FLYOUT_CHECK_PINNED =
  "pointer-events-none absolute right-3 top-1/2 inline-flex h-5 w-5 -translate-y-1/2 items-center " +
  "justify-center text-accent group-hover/conn:hidden";
export const FLYOUT_EMPTY = "type-helper text-faint px-3 py-2";
export const FLYOUT_ADD = menuRowClass({ extra: "text-secondary" });

/** The sign-out control: a SIBLING of the row button (buttons cannot nest), floated over the
 *  row's reserved right padding. It takes the row's LAST lane, the one the tick occupies at
 *  rest -- the tick hides for the hover, so the three controls end flush with the row's end
 *  rather than one slot short of it. */
export const ROW_TRASH =
  "absolute right-3 top-1/2 hidden h-5 w-5 -translate-y-1/2 cursor-pointer items-center " +
  "justify-center rounded text-faint transition-colors hover:text-danger group-hover/conn:inline-flex";
/** Armed: stays visible whether or not the row is hovered, and says what it will do. Same right
 *  edge as the bin it replaces, so arming it does not shift anything. */
/** Armed, it is wider than the bin it replaces, so it carries the row's own background: it may
 *  overhang the provider name rather than forcing every row to reserve space for a word that is
 *  almost never shown. */
/** The rename control: a SIBLING of the row button like the bin, one slot further in, so each
 *  control keeps its own lane and none of them ever displaces another. */
export const ROW_PENCIL =
  "absolute right-9 top-1/2 hidden h-5 w-5 -translate-y-1/2 cursor-pointer items-center " +
  "justify-center rounded text-faint transition-colors hover:text-primary group-hover/conn:inline-flex";
/** The default toggle, in the outermost lane, shown on hover like the pencil and the bin. */
export const ROW_STAR =
  "absolute right-15 top-1/2 hidden h-5 w-5 -translate-y-1/2 cursor-pointer items-center " +
  "justify-center rounded text-faint transition-colors hover:text-primary group-hover/conn:inline-flex";
/** The star of the account new chats open on: visible whether or not the row is hovered, since
 *  it is a fact about the account rather than a control that only matters under a pointer.
 *
 *  Its resting lane depends on whether the row also carries a tick. With one it sits BESIDE the
 *  tick, one lane in, so the row's two marks read as a pair at its end. Without one it takes
 *  the last lane itself: a lone mark stopping a slot short of the row's end reads as
 *  misaligned, not as room held for something that is not there. On hover it steps out to the
 *  control lane it shares with the pencil and the bin either way, which is the only way a group
 *  flush with the row's end can also keep its own order.
 *
 *  `fill-current` rather than the icon's own `filled`, which drops the stroke: a star painted
 *  by its fill alone is a stroke-width smaller all round than the outline beside it, so the
 *  marked row's star read as the smaller of the two. Filling the outlined glyph keeps one
 *  silhouette and changes only what is inside it. */
export function rowStarPinnedClass(hasTick: boolean): string {
  return (
    `absolute ${hasTick ? "right-9" : "right-3"} group-hover/conn:right-15 top-1/2 inline-flex ` +
    "h-5 w-5 -translate-y-1/2 cursor-pointer items-center justify-center rounded text-accent " +
    "transition-colors hover:text-primary [&>svg]:fill-current"
  );
}
/** The row mid-rename: the field takes the whole row, since every control is hidden while it
 *  is up. The wrapper insets the bordered field from the card's full-bleed edges; the field
 *  keeps the row's height, so nothing shifts on the way in or out. */
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
