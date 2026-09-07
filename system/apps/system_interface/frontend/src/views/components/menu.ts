/* The floating-menu chrome: a card on the primary surface with a hairline
 * border, 8px radius and the overlay elevation shadow, holding 32px rows of
 * full-bleed hover highlight. Every floating menu composes this recipe --
 * the tab ⋮ menu, the rail's row menus, the launcher's filter menu, and the
 * model card with its flyouts (whose selected/locked row variants extend the
 * row shape in modelCardStyles.ts).
 *
 * Positioning is not part of the recipe -- callers say fixed/absolute in
 * `extra`, along with min-width and text size. The Tailwind scanner reads
 * utility names from the literals in this file (style.css's `@source` covers
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

export function menuRowClass(options: MenuRowOptions = {}): string {
  const parts = [
    `flex h-8 w-full items-center px-3 text-left hover:bg-fill-hover ${MENU_ROW_FOCUS}`,
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
