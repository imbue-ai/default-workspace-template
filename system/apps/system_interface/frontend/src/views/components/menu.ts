/* The floating-menu chrome: a card on the primary surface with a hairline
 * border, 8px radius and the overlay elevation shadow, holding 32px rows of
 * icon + label. ModelBar's dropdown is a deliberate cousin (sticky layer,
 * denser padding, block-shaped options) and keeps its own recipe.
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
  extra?: string;
}

export function menuRowClass(options: MenuRowOptions = {}): string {
  const parts = [
    "flex h-8 w-full cursor-pointer items-center px-3 text-left hover:bg-fill-hover",
    options.tightGap === true ? "gap-1" : "gap-2",
  ];
  if (options.extra !== undefined && options.extra !== "") parts.push(options.extra);
  return parts.join(" ");
}
