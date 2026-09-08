import { describe, expect, it } from "vitest";

import { isInSafeTriangle, placeFlyout } from "./flyout-position";

/** A card open near the bottom of a 1280x800 window, which is where the composer puts it.
 *  `rowTop` is the row that opened the flyout; `contentHeight` is a short four-row list. */
const BASE = {
  cardLeft: 400,
  cardWidth: 340,
  rowTop: 528,
  flyoutPadding: 5,
  contentHeight: 138,
  flyoutWidth: 300,
  maxFlyoutHeight: 368,
  viewportWidth: 1280,
  viewportHeight: 800,
  margin: 8,
  overlap: 4,
};

describe("placeFlyout", () => {
  it("tucks under the card's right edge and lines its first row up with the row", () => {
    // top is `rowTop - flyoutPadding`, which puts the flyout's first ROW on `rowTop`.
    expect(placeFlyout(BASE)).toMatchObject({ left: 736, top: 523, side: "trailing", isSlid: false });
  });

  it("slides up rather than being squeezed by the space below the row", () => {
    // The whole reason this file exists: the card opens from the composer at the bottom of the
    // panel, so a ten-row catalog opened from a low row has nothing below it to grow into.
    const tall = placeFlyout({ ...BASE, rowTop: 700, contentHeight: 368 });
    expect(tall.isSlid).toBe(true);
    // Slid up by exactly enough to stand on the bottom margin, and no shorter for it.
    expect(tall.top).toBe(800 - 8 - 368);
    expect(tall.maxHeight).toBe(368);
  });

  it("slides by only as much as it has to", () => {
    // 21px short of fitting, so it moves 21px -- not to some fixed anchor.
    const barely = placeFlyout({ ...BASE, rowTop: 680 });
    expect(barely.top).toBe(800 - 8 - 138);
    expect(barely.isSlid).toBe(true);
  });

  it("holds the alignment whenever the box fits", () => {
    const roomy = placeFlyout({ ...BASE, rowTop: 200 });
    expect(roomy.top).toBe(195);
    expect(roomy.isSlid).toBe(false);
  });

  it("caps a list too tall for the window, and measures the slide against the cap", () => {
    const huge = placeFlyout({ ...BASE, rowTop: 700, contentHeight: 5000, maxFlyoutHeight: 5000 });
    // Capped to the window less both margins...
    expect(huge.maxHeight).toBe(784);
    // ...and slid to the top margin, rather than to where 5000px of content would have put it.
    expect(huge.top).toBe(8);
  });

  it("flips to the leading side when the trailing side would not fit", () => {
    const placed = placeFlyout({ ...BASE, viewportWidth: 900 });
    expect(placed.side).toBe("leading");
    expect(placed.left).toBe(104);
  });

  it("pins inside the viewport when neither side fits", () => {
    const placed = placeFlyout({ ...BASE, cardLeft: 20, viewportWidth: 400 });
    expect(placed.left).toBeGreaterThanOrEqual(8);
    expect(placed.left + BASE.flyoutWidth).toBeLessThanOrEqual(400);
  });

  it("keeps the box on screen when the row is off it", () => {
    const placed = placeFlyout({ ...BASE, rowTop: -50 });
    expect(placed.top).toBe(8);
  });
});

/** A flyout on the card's trailing side: its near edge is a vertical line at x=740, running
 *  from y=300 down to y=560, and the pointer set off from a row at (700, 550). */
const APEX = { x: 700, y: 550 };
const EDGE = { edgeX: 740, top: 300, bottom: 560 };

describe("isInSafeTriangle", () => {
  it("holds a pointer heading up and across towards the flyout", () => {
    expect(isInSafeTriangle({ x: 720, y: 480 }, APEX, EDGE)).toBe(true);
  });

  it("lets go of a pointer heading straight up the card instead", () => {
    expect(isInSafeTriangle({ x: 640, y: 480 }, APEX, EDGE)).toBe(false);
  });

  it("lets go once the pointer is past the flyout's edge", () => {
    expect(isInSafeTriangle({ x: 900, y: 480 }, APEX, EDGE)).toBe(false);
  });

  it("holds the apex itself and the edge's corners", () => {
    expect(isInSafeTriangle(APEX, APEX, EDGE)).toBe(true);
    expect(isInSafeTriangle({ x: 740, y: 300 }, APEX, EDGE)).toBe(true);
    expect(isInSafeTriangle({ x: 740, y: 560 }, APEX, EDGE)).toBe(true);
  });

  it("holds nothing off the line when the flyout has no height to aim at", () => {
    const flat = { edgeX: 740, top: 400, bottom: 400 };
    expect(isInSafeTriangle({ x: 720, y: 480 }, APEX, flat)).toBe(false);
  });
});
