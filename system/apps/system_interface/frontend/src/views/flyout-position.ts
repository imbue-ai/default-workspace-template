/**
 * Pure geometry for a side flyout -- a submenu that opens BESIDE its parent panel,
 * top-aligned with the row that triggered it.
 *
 * Sibling of `dropdown-position.ts`, which owns the horizontal-only case (a popup under
 * its trigger). A flyout needs both axes and a height cap, and it opens against a panel
 * rather than a label, so the rules are different enough to keep apart.
 *
 * Kept free of the DOM so it is unit-testable; the caller measures the live rects and
 * feeds them in.
 *
 * Three rules, in priority order:
 *   1. HARD: the flyout stays inside the viewport, with `margin` on every edge.
 *   2. SOFT: it top-aligns with the row that opened it (system-menu behaviour) and tucks
 *      slightly UNDER the parent's trailing edge rather than floating off it with a gap.
 *   3. LAST RESORT: when there is no room on the trailing side, it flips to the leading
 *      side -- a flyout half off-screen is worse than one on the other side.
 */

export interface FlyoutPlacementInput {
  /** Viewport rect of the panel the flyout opens beside. */
  parent: { left: number; right: number };
  /** Viewport y of the top edge of the row that opened the flyout. */
  rowTop: number;
  /** Measured (or intended) size of the flyout box. */
  flyoutWidth: number;
  /** The tallest the flyout may ever be, before the viewport cap. */
  maxFlyoutHeight: number;
  viewportWidth: number;
  viewportHeight: number;
  /** Gap to keep between the flyout and each viewport edge. */
  margin: number;
  /** How far the flyout tucks under the parent's edge. Positive overlaps. */
  overlap: number;
}

export interface FlyoutPlacement {
  left: number;
  top: number;
  /** Cap, not a fixed height: the content decides, up to this. */
  maxHeight: number;
  /** Which side it ended up on, so the caller can mirror a shadow or an arrow. */
  side: "trailing" | "leading";
}

export function placeFlyout(input: FlyoutPlacementInput): FlyoutPlacement {
  const { parent, rowTop, flyoutWidth, maxFlyoutHeight, viewportWidth, viewportHeight, margin, overlap } = input;

  const trailing = parent.right - overlap;
  const leading = parent.left + overlap - flyoutWidth;
  // Prefer trailing; flip only when the whole box would not fit there but WOULD fit leading.
  const fitsTrailing = trailing + flyoutWidth <= viewportWidth - margin;
  const fitsLeading = leading >= margin;
  const side: "trailing" | "leading" = fitsTrailing || !fitsLeading ? "trailing" : "leading";
  const wanted = side === "trailing" ? trailing : leading;
  // Rule 1 still binds on the chosen side: at absurd viewport widths neither side fits, and
  // pinning to the left margin keeps the first characters readable.
  const left = Math.min(Math.max(wanted, margin), Math.max(margin, viewportWidth - margin - flyoutWidth));

  const room = viewportHeight - margin - rowTop;
  // Top-aligning a row near the bottom would leave a sliver, so lift the flyout enough to
  // show either its full height or everything the viewport can hold, whichever is smaller.
  const height = Math.min(maxFlyoutHeight, viewportHeight - 2 * margin);
  const top = room >= height ? rowTop : Math.max(margin, viewportHeight - margin - height);
  return { left, top, maxHeight: Math.min(maxFlyoutHeight, viewportHeight - margin - top), side };
}
