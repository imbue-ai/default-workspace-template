import { describe, expect, it } from "vitest";

import { placeFlyout } from "./flyout-position";

/** A card open near the bottom of a 1280x800 window, which is where the composer puts it. */
const BASE = {
  cardLeft: 400,
  cardWidth: 340,
  rowBottom: 560,
  flyoutWidth: 300,
  maxFlyoutHeight: 334,
  viewportWidth: 1280,
  viewportHeight: 800,
  margin: 8,
  overlap: 4,
};

describe("placeFlyout", () => {
  it("tucks under the card's right edge and stands on the row", () => {
    expect(placeFlyout(BASE)).toMatchObject({ left: 736, bottom: 240, side: "trailing" });
  });

  it("grows upward rather than being squeezed by the space below the row", () => {
    // The whole reason this file diverges from the mockup: the card opens from the composer at
    // the bottom of the panel, so downward there is nothing -- 560px of room sits ABOVE.
    const nearBottom = placeFlyout({ ...BASE, rowBottom: 780 });
    expect(nearBottom.bottom).toBe(20);
    expect(nearBottom.maxHeight).toBe(334);
  });

  it("caps the height when the row is near the top instead of overflowing", () => {
    const nearTop = placeFlyout({ ...BASE, rowBottom: 100 });
    expect(nearTop.maxHeight).toBe(92);
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

  it("keeps the base on screen when the row is off it", () => {
    const placed = placeFlyout({ ...BASE, rowBottom: -50 });
    expect(placed.bottom).toBe(792);
    expect(placed.maxHeight).toBe(0);
  });
});
