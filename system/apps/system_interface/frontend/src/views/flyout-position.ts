/**
 * Pure geometry for the combo card's side flyout, ported from the mockup.
 *
 * The mockup's rule, verbatim (`ModelBar.tsx`): the flyout's left edge tucks 4px UNDER the
 * card's right edge (macOS submenu geometry -- a gap reads as two separate panels), its top
 * aligns with the row that opened it minus 6px, and its height is capped to whatever space
 * remains below that top.
 *
 * An earlier version of this file also LIFTED a flyout opened from a row near the bottom so
 * its full height would fit. That was invented, not ported, and it is what put the list in the
 * top-right corner of the screen while the card sat at the bottom: capping the height keeps the
 * flyout beside the row that opened it, which is the whole point of a submenu.
 *
 * Kept free of the DOM so it is unit-testable; the caller measures and feeds it in.
 */

export interface FlyoutPlacementInput {
  /** Viewport left of the card, and its width. */
  cardLeft: number;
  cardWidth: number;
  /** Viewport y of the top edge of the row that opened the flyout. */
  rowTop: number;
  flyoutWidth: number;
  /** The tallest the flyout may be before the viewport caps it. */
  maxFlyoutHeight: number;
  viewportWidth: number;
  viewportHeight: number;
  /** Gap kept between the flyout and each viewport edge. */
  margin: number;
  /** How far the flyout tucks under the card's edge. Positive overlaps. */
  overlap: number;
}

export interface FlyoutPlacement {
  left: number;
  top: number;
  /** A cap, not a height: the content decides, up to this. */
  maxHeight: number;
  side: "trailing" | "leading";
}

export function placeFlyout(input: FlyoutPlacementInput): FlyoutPlacement {
  const { cardLeft, cardWidth, rowTop, flyoutWidth, maxFlyoutHeight } = input;
  const { viewportWidth, viewportHeight, margin, overlap } = input;

  const trailing = cardLeft + cardWidth - overlap;
  const leading = cardLeft + overlap - flyoutWidth;
  // Prefer the trailing side; flip only when the box would not fit there but would fit on the
  // other. A flyout half off-screen is worse than one on the unexpected side.
  const fitsTrailing = trailing + flyoutWidth <= viewportWidth - margin;
  const side: "trailing" | "leading" = fitsTrailing || leading < margin ? "trailing" : "leading";
  const wanted = side === "trailing" ? trailing : leading;
  // At absurd viewport widths neither side fits; pin to the left margin so the first
  // characters stay readable.
  const left = Math.min(Math.max(wanted, margin), Math.max(margin, viewportWidth - margin - flyoutWidth));

  // Top-aligned with the row, never above the viewport. The height gives way, not the position.
  const top = Math.max(margin, rowTop);
  return { left, top, maxHeight: Math.max(0, Math.min(maxFlyoutHeight, viewportHeight - top - margin)), side };
}
