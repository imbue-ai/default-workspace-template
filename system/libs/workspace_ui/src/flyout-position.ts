/**
 * Pure geometry for the combo card's side flyout: where it sits (`placeFlyout`), and the
 * wedge a pointer on its way to it is allowed to cross (`isInSafeTriangle`).
 *
 * The flyout's BASE sits level with the row that opened it and the list grows UPWARD. That is
 * not the ordinary top-align-and-cap-downward rule, and the reason is that this card
 * opens from the composer at the BOTTOM of the panel: a list capped by the space below its row
 * would have roughly three rows to work with, far too few for a thousand-model catalog.
 * Growing up gives it the whole window instead.
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

/** A viewport point -- where the pointer is, or where it was. */
export interface FlyoutPoint {
  x: number;
  y: number;
}

/** The flyout edge a pointer travelling towards it must cross: the side FACING the card,
 *  and that side's full vertical span. */
export interface SafeTriangleBase {
  edgeX: number;
  top: number;
  bottom: number;
}

/**
 * The safe triangle: is `point` inside the wedge between `apex` and the open flyout's near edge?
 *
 * A hover menu has one hard problem. The flyout opens beside the card, so the pointer has to
 * travel diagonally to reach it -- and on the way it crosses the card's OTHER rows, each of
 * which would otherwise take the hover and replace the flyout being aimed at. Waiting longer
 * before switching does not fix it: the pointer is genuinely resting on those rows.
 *
 * What tells travel apart from a change of mind is direction, and the triangle is direction
 * made testable. Its apex is the last point the pointer occupied on the row that opened the
 * flyout; its base is the flyout's near edge. Every path from that point to that edge stays
 * inside it, and a pointer heading anywhere else leaves it almost at once.
 *
 * Kept here beside `placeFlyout`, and pure for the same reason: the caller measures.
 */
export function isInSafeTriangle(point: FlyoutPoint, apex: FlyoutPoint, base: SafeTriangleBase): boolean {
  const vertices: readonly FlyoutPoint[] = [apex, { x: base.edgeX, y: base.top }, { x: base.edgeX, y: base.bottom }];
  // Inside iff `point` sits on the same side of all three edges, walked in order -- so the
  // cross products never disagree in sign. A degenerate triangle (an apex already on the
  // edge, or a flyout of no height) contains only its own line, which reads as "not
  // travelling" and simply leaves the rows unprotected.
  let anyPositive = false;
  let anyNegative = false;
  for (let index = 0; index < vertices.length; index++) {
    const from = vertices[index];
    const to = vertices[(index + 1) % vertices.length];
    const cross = (to.x - from.x) * (point.y - from.y) - (to.y - from.y) * (point.x - from.x);
    if (cross > 0) anyPositive = true;
    if (cross < 0) anyNegative = true;
  }
  return !(anyPositive && anyNegative);
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
