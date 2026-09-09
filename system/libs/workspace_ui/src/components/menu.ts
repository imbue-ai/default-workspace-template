/* The floating-menu chrome: a card on the primary surface with a hairline
 * border, 8px radius and the overlay elevation shadow, holding 32px rows
 * whose highlight is an inset, rounded slab rather than a full-bleed band.
 * Every floating menu composes this recipe -- the tab ⋮ menu, the rail's row
 * menus, the launcher's filter menu, and the model card with its flyouts
 * (whose selected/locked row variants extend the row shape in
 * modelCardStyles.ts).
 *
 * Positioning is not part of the recipe -- callers say fixed/absolute in
 * `extra`, along with min-width and text size. The Tailwind scanner reads
 * utility names from the literals in this file (base.css's `@source` covers
 * every .ts file): keep every utility name a contiguous literal. */

export function menuCardClass(extra = ""): string {
  const parts = ["z-(--z-dropdown) rounded-lg border border-default bg-surface py-1 shadow-overlay"];
  if (extra !== "") parts.push(extra);
  return parts.join(" ");
}

export interface MenuRowOptions {
  /** 4px row gap instead of the default 8px. */
  tightGap?: boolean;
  /** A row that highlights but does not act (e.g. a read-only value): default
   *  arrow instead of the pointer. */
  inert?: boolean;
  extra?: string;
}

/** The keyboard-focus treatment for a focusable row (a real <button>). Inset so
 *  the ring stays inside the card instead of the OS default halo overhanging it.
 *  Inert on a non-focusable row (the tab menu's divs), so it rides the base. */
const MENU_ROW_FOCUS = "focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent";

/** The row's highlight is a slab inset 4px from the card's edges, with the 4px
 *  radius that the card's own 8px corner leaves once you step 4px inward -- so
 *  the highlight looks concentric with the card rather than pasted into it.
 *
 *  The width is spelled `calc(100% - 0.5rem)` rather than left to `w-full` or
 *  to `auto`, because neither is right once the row has margins. `w-full`
 *  measures 100% PLUS the 8px and overflows the card (invisibly -- the card
 *  clips). And `auto` does not fill: a <button> shrink-to-fits even at
 *  `display: flex`, which pulls the highlight in behind a trailing tick and
 *  leaves it outside the band. An explicit width is the only form that holds
 *  for a <button>, a <label> and the tab menu's <div>s alike.
 *
 *  `px-2` completes the 12px the text used to get from `px-3` alone. The
 *  highlight moved inward; the text did not. */
const MENU_ROW_SLAB = "mx-1 w-[calc(100%-0.5rem)] rounded px-2";

export function menuRowClass(options: MenuRowOptions = {}): string {
  const parts = [
    `flex h-8 items-center ${MENU_ROW_SLAB} text-left hover:bg-fill-hover ${MENU_ROW_FOCUS}`,
    options.inert === true ? "cursor-default" : "cursor-pointer",
    options.tightGap === true ? "gap-1" : "gap-2",
  ];
  if (options.extra !== undefined && options.extra !== "") parts.push(options.extra);
  return parts.join(" ");
}

/** The rule between two row groups. Full-bleed, since the card pads only
 *  vertically. */
export function menuDividerClass(): string {
  return "my-1 border-t border-default";
}
