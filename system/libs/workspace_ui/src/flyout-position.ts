/**
 * Pure geometry for the combo card's side flyout: where it sits (`placeFlyout`), and the
 * wedge a pointer on its way to it is allowed to cross (`isInSafeTriangle`).
 *
 * The flyout's FIRST ROW lines up with the row that opened it, so the row you pointed at and
 * the list it produced read as one line continuing sideways. That alignment is the default and
 * the flyout keeps it whenever it can.
 *
 * When it cannot -- the card opens from the composer at the BOTTOM of the panel, so a long
 * model list starting level with a low row would run off the screen -- the flyout SLIDES UP,
 * by exactly as much as it takes to fit, and no further. It never gives up height to hold the
 * alignment: a list squeezed into the space below its own row would have about three rows to
 * work with, far too few for a thousand-model catalog, so the slide is what buys it the whole
 * window. Only a list too tall for the window at all is capped, and then it scrolls.
 *
 * Kept free of the DOM so it is unit-testable; the caller measures and feeds it in.
 */

export interface FlyoutPlacementInput {
  /** Viewport left of the card, and its width. */
  cardLeft: number;
  cardWidth: number;
  /** Viewport y of the TOP edge of the row that opened the flyout: what the flyout's first
   *  row lines up with. */
  rowTop: number;
  flyoutWidth: number;
  /** The distance from the flyout's outer top edge to the top of its first row -- its border
   *  and its padding -- so `rowTop` aligns the ROW rather than the box that carries it. */
  flyoutPadding: number;
  /** How tall the flyout wants to be, from what it is about to hold. Decides whether it has
   *  to slide, and by how much. */
  contentHeight: number;
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
  /** Viewport y of the flyout's TOP edge. */
  top: number;
  /** A cap, not a height: the content decides, up to this. */
  maxHeight: number;
  side: "trailing" | "leading";
  /** Whether the alignment had to give way to fit the box on screen. Nothing positions off
   *  this; it is here so a test can say which of the two rules it is exercising. */
  isSlid: boolean;
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
  const { cardLeft, cardWidth, rowTop, flyoutPadding, flyoutWidth } = input;
  const { contentHeight, maxFlyoutHeight, viewportWidth, viewportHeight, margin, overlap } = input;

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

  // The height the box will actually occupy: what it wants, capped by its own ten-row limit
  // and by the window. The slide is measured against THIS rather than against the content,
  // so a list already capped to a scroller does not slide for height it will never use.
  const cap = Math.max(0, Math.min(maxFlyoutHeight, viewportHeight - 2 * margin));
  const height = Math.min(contentHeight, cap);
  // Aligned: the box sits `flyoutPadding` above the row, which puts its first row ON the row.
  const aligned = rowTop - flyoutPadding;
  // The lowest top that still leaves the whole box on screen. Sliding UP to reach it is what
  // a low row gets instead of a squeezed list.
  const lowestFitting = viewportHeight - margin - height;
  const top = Math.max(margin, Math.min(aligned, lowestFitting));
  return {
    left,
    top,
    maxHeight: cap,
    side,
    isSlid: top !== aligned,
  };
}
