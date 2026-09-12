/**
 * The model/provider menu's own class strings: what is left after the shared menu.
 *
 * The menu's frame, rows, divider, sheet and submenus are the workspace's `Menu`
 * (`components/menu`), so this menu behaves and dresses like the tab menu and the
 * rail's row menus. What lives here is the part no other menu has: the composer
 * chip that opens it, the effort slider, the fast-mode switch, the model list's
 * search field, and the row geometry that reserves a lane for each of an account
 * row's trailing controls.
 *
 * Every colour, size and elevation below comes from the semantic utility layer,
 * the `type-*` roles and the shadow tokens. Tailwind v4 emits nothing for an
 * unknown utility, so a name this app does not define is a silent no-op;
 * `style-modules.test.ts` guards against that.
 */

// The menu
/** The menu and its submenus are one width: a submenu narrower than the menu it slides out of
 *  reads as a mistake at the seam where they meet. The effort slider is a fixed 128px and
 *  needs none of this. */
export const MENU_WIDTH = 300;

/** Show ten rows before the model list starts scrolling. Fewer and a long catalog reads as a
 *  keyhole; many more and the submenu is a wall. Derived rather than guessed so it stays true
 *  if the row height changes. */
const SUBMENU_ROW_HEIGHT = 32;
const SUBMENU_VISIBLE_ROWS = 10;
/** The submenu card's border and vertical padding, above and below the rows. */
const SUBMENU_CHROME = 2 * (1 + 4);
/** `SEARCH_INPUT_EXTRA`'s `h-8` plus `SEARCH_WRAP`'s `mb-1.5` under it. */
const SEARCH_FIELD_HEIGHT = 32 + 6;
/** Ten rows, plus the search field standing over them, plus the box's own chrome. */
export const MODEL_SUBMENU_MAX_HEIGHT =
  SUBMENU_CHROME + SUBMENU_VISIBLE_ROWS * SUBMENU_ROW_HEIGHT + SEARCH_FIELD_HEIGHT;

// The composer trigger
export const TRIGGER =
  "flex h-[30px] items-center gap-1 rounded-lg px-2 type-helper whitespace-nowrap " +
  "text-faint transition-colors hover:bg-fill-hover hover:text-secondary cursor-pointer";
/** The separators between the chip's three parts, a step quieter than the values. */
export const TRIGGER_DOT = "text-faint/60";

// The rows that are not plain rows
/** A row that holds a control rather than a value: the same height and padding as the shared
 *  row, without its highlight -- a slider is not something one picks. */
export const ROW_STATIC = "flex h-8 items-center gap-2 px-3";
/** Labels sit at the values' own size, one colour step back -- the menu reads as a spec sheet. */
export const ROW_LABEL = "text-secondary";
export const ROW_VALUE_STATIC = "ml-auto flex items-center gap-2";
/** Marks a row group as a tooltip host. */
export const ROW_WRAP = "group/conn relative";

// The effort slider
export const EFFORT_VALUE = "type-helper text-primary";
/** Wraps the track so the level dots can be positioned over it; a bare slider gives no clue
 *  where the levels are. */
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
const SLIDER_TICK_SHAPE = "absolute top-0 h-[2px] w-[2px] -translate-x-1/2 -translate-y-1/2 rounded-full";
/** A dot below the thumb, over the filled part of the track: the surface colour, since what it
 *  is drawn on is the green rather than the track. */
export const SLIDER_TICK_ON_FILL = `${SLIDER_TICK_SHAPE} bg-surface/70`;
/** A dot above the thumb, over the unfilled track. */
export const SLIDER_TICK_ON_TRACK = `${SLIDER_TICK_SHAPE} bg-primary/70`;
export const SLIDER =
  "relative h-[6px] w-full cursor-pointer appearance-none rounded-full " +
  "[&::-moz-range-thumb]:h-3 [&::-moz-range-thumb]:w-3 [&::-moz-range-thumb]:rounded-full " +
  "[&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:bg-surface " +
  "[&::-moz-range-thumb]:shadow-[0_0_0_1px_rgba(0,0,0,0.15),0_1px_2px_rgba(0,0,0,0.25)] " +
  "[&::-webkit-slider-thumb]:h-3 [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:appearance-none " +
  "[&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-surface " +
  "[&::-webkit-slider-thumb]:shadow-[0_0_0_1px_rgba(0,0,0,0.15),0_1px_2px_rgba(0,0,0,0.25)]";

// The fast switch
/** The switch comes in two sizes, and a size is a track plus its throw, handed out together:
 *  a track scaled on its own leaves the knob's `translate-x-[...]` throw sized for the old
 *  track, and the knob lands outside it.
 *
 *  Each row is arithmetic: the knob is the track's height less 2px of inset top and bottom,
 *  the on-position is `width - inset - knob`, which leaves the knob the same 2px from either
 *  end, and the tick is the fraction of the knob that leaves it a rim.
 *
 *  Both switches in the chat are `sm`: the composer's under-bar one, beside a line of helper
 *  text, and the menu's fast-mode row, in a column of values. `md` is the full-size step. */
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
 *  12px one, the same rim either way. */
export function switchCheckSize(size: SwitchSize): number {
  return SWITCH_SIZES[size].check;
}

export const SWITCH_ON = "bg-accent";
export const SWITCH_OFF = "bg-fill-active";
/** The same 2px at rest whatever the size, so it is not in the table. */
const SWITCH_KNOB_OFF = "translate-x-[2px]";
export const SWITCH_CHECK = "text-accent";

// The submenus' own content
/** The search field at the head of the model list. The wrapper positions the magnifier over the
 *  field's own left padding; the field itself is the shared `inputClass`, so its frame, focus
 *  ring and placeholder match every other text field in the workspace. The margin is BELOW it,
 *  separating it from the list it filters -- above it the submenu's own padding is the gap. */
export const SEARCH_WRAP = "relative mx-1.5 mb-1.5";
export const SEARCH_ICON = "pointer-events-none absolute left-2.5 top-1/2 z-(--z-content) -translate-y-1/2 text-faint";
/** Room for the magnifier, and the dense-chrome row size the rest of the submenu sits at. */
export const SEARCH_INPUT_EXTRA = "h-8 py-0 pl-8 text-(length:--font-size-row)";
/** The list under the search field, the part of the submenu that scrolls. A slim, always-visible
 *  track: the list is anchored at its base and grows upward, so its top edge is not somewhere
 *  the eye naturally checks for "more", and an overlay scrollbar that only appears mid-scroll
 *  says nothing until it is too late (the track's own rules are in style.css). */
export const SUBMENU_SCROLL = "model-submenu-scroll min-h-0 flex-1 overflow-y-auto overscroll-contain";

/** The shared row shape (`menuRowClass`), restated: every submenu row is the same 32px slab
 *  with the same inset highlight, and the selected one keeps a steady fill. Kept in step with
 *  `menuRowClass` -- `mx-1 w-[calc(100%-0.5rem)] rounded-[12px] px-2` is its inset highlight
 *  slab, and the reasoning for each part lives there. */
const SUBMENU_ROW_SHAPE =
  "flex h-8 items-center gap-1.5 mx-1 w-[calc(100%-0.5rem)] rounded-[12px] px-2 text-left " +
  "focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent";
/** An ACCOUNT row reserves its right edge for the star, the rename pencil and the bin, which are
 *  positioned against the row's WRAPPER rather than the row, so they stay put while the
 *  highlight insets around them, and the provider name never reflows when they appear. 80px is
 *  the outermost of the three at its far edge (`right-15` plus its own 20px), so the reserve
 *  says exactly what is in the row and not a pixel more. Model rows do not reserve it: they
 *  carry only the tick, and the reserve would truncate every model name for controls never
 *  drawn on them. */
const ACCOUNT_ROW_BASE = `${SUBMENU_ROW_SHAPE} pr-20`;
export const SUBMENU_ROW = `${SUBMENU_ROW_SHAPE} text-primary hover:bg-fill-hover cursor-pointer`;
export const SUBMENU_ROW_SELECTED = `${SUBMENU_ROW_SHAPE} bg-fill-active text-primary cursor-pointer`;
/** Lit from the WRAPPER, not the row button. The star, the pencil and the bin are SIBLINGS of
 *  that button, so a pointer on one of them is not on the row by CSS's reckoning -- and the
 *  row would drop its highlight at exactly the moment the pointer arrived at the controls that
 *  highlight had just revealed. */
export const ACCOUNT_ROW = `${ACCOUNT_ROW_BASE} text-primary group-hover/conn:bg-fill-hover cursor-pointer`;
export const ACCOUNT_ROW_SELECTED = `${ACCOUNT_ROW_BASE} bg-fill-active text-primary cursor-pointer`;
export const SUBMENU_ROW_NAME = "truncate";
export const SUBMENU_ROW_SUB = "type-helper text-faint";
/** The MODEL row's tick: the last thing in the row, pushed out by `ml-auto` and so landing on
 *  the row's own `px-2` padding edge -- which is where the account rows' pinned tick sits too,
 *  13px in from the submenu's edge, so the two lists' ticks share one line. Both ticks stand in
 *  the same 20x20 box the row's control buttons do, centred in it: a bare 13px glyph and a 13px
 *  glyph centred in a 20px button do not share a centre line even when their boxes end on the
 *  same pixel, since the button's padding puts its glyph 3.5px further in. */
export const SUBMENU_CHECK = "ml-auto inline-flex h-5 w-5 shrink-0 items-center justify-center text-accent";
/** The same tick on a row that also carries controls: pinned to the row's right edge as a
 *  SIBLING of the button, since buttons cannot nest.
 *
 *  It stands down while the row is hovered. The three controls want the row's end -- a control
 *  group that stops short of the edge to leave a mark room reads as misaligned -- and the tick
 *  is the one thing there that can afford to go: what it says is still said by the row's own
 *  selected fill, and it comes straight back when the pointer leaves. */
export const SUBMENU_CHECK_PINNED =
  "pointer-events-none absolute right-3 top-1/2 inline-flex h-5 w-5 -translate-y-1/2 items-center " +
  "justify-center text-accent group-hover/conn:hidden";
export const SUBMENU_EMPTY = "type-helper text-faint px-3 py-2";
/** "+ Add a provider" under the account list: the shared row, one colour step back. */
export const SUBMENU_ADD = `${SUBMENU_ROW_SHAPE} gap-2 text-secondary hover:bg-fill-hover cursor-pointer`;

/** The sign-out control: a SIBLING of the row button (buttons cannot nest), floated over the
 *  row's reserved right padding. It takes the row's LAST lane, the one the tick occupies at
 *  rest -- the tick hides for the hover, so the three controls end flush with the row's end
 *  rather than one slot short of it. */
export const ROW_TRASH =
  "absolute right-3 top-1/2 hidden h-5 w-5 -translate-y-1/2 cursor-pointer items-center " +
  "justify-center rounded text-faint transition-colors hover:text-danger group-hover/conn:inline-flex";
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
 *  by its fill alone is a stroke-width smaller all round than the outline beside it. Filling
 *  the outlined glyph keeps one silhouette and changes only what is inside it. */
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

/* No tooltip classes here: a CSS bubble inside the menu is clipped by the submenu's
 * `overflow-hidden`, and the menu and the submenu are fixed boxes that are each their own
 * stacking context, so a bubble in one cannot rise above the other. Rows spread
 * `hoverTooltipAttrs(...)` from `hoverTooltip.ts` instead: a single fixed bubble on <body>.
 */
