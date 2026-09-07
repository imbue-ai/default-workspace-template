/**
 * Pure geometry for the combo card's side flyout.
 *
 * The flyout's BASE sits level with the row that opened it and the list grows UPWARD. That is
 * not the ordinary top-align-and-cap-downward rule, and the reason is that this card
 * opens from the composer at the BOTTOM of the panel: a list capped by the space below its row
 * had roughly three rows to work with, which is what pi's thousand-model catalog was being
 * squeezed into. Growing up gives it the whole window instead.
 *
 * The search field belongs at the bottom of that column for the same reason -- it stays put,
 * next to the row you came from, while the list extends away from your hand.
 *
 * Kept free of the DOM so it is unit-testable; the caller measures and feeds it in.
 */

export interface FlyoutPlacementInput {
  /** Viewport left of the card, and its width. */
  cardLeft: number;
  cardWidth: number;
  /** Viewport y of the BOTTOM edge of the row that opened the flyout: the base to sit on. */
  rowBottom: number;
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
  /** Distance from the viewport's BOTTOM to the flyout's base -- it is anchored there and
   *  grows upward, so this is what stays fixed as the content changes. */
  bottom: number;
  /** A cap, not a height: the content decides, up to this. */
  maxHeight: number;
  side: "trailing" | "leading";
}

export function placeFlyout(input: FlyoutPlacementInput): FlyoutPlacement {
  const { cardLeft, cardWidth, rowBottom, flyoutWidth, maxFlyoutHeight } = input;
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

  // The base never leaves the viewport, and never sits so low the flyout has nowhere to grow.
  const base = Math.min(Math.max(rowBottom, margin), viewportHeight - margin);
  return {
    left,
    bottom: viewportHeight - base,
    // Everything between the base and the top margin is available to grow into.
    maxHeight: Math.max(0, Math.min(maxFlyoutHeight, base - margin)),
    side,
  };
}
