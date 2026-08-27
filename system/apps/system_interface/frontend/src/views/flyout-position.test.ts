import { describe, expect, it } from "vitest";

import { placeFlyout } from "./flyout-position";

const BASE = {
  cardLeft: 400,
  cardWidth: 340,
  rowTop: 300,
  flyoutWidth: 280,
  maxFlyoutHeight: 420,
  viewportWidth: 1400,
  viewportHeight: 900,
  margin: 12,
  overlap: 4,
};

describe("placeFlyout", () => {
  it("tucks under the card's trailing edge and top-aligns with the row", () => {
    expect(placeFlyout(BASE)).toMatchObject({ left: 736, top: 300, side: "trailing" });
  });

  it("stays beside the row when it opens near the bottom, capping its height instead", () => {
    // The bug this file was rewritten for: lifting the flyout so its full height fits put the
    // list in the opposite corner from the card that opened it.
    const placed = placeFlyout({ ...BASE, rowTop: 700 });
    expect(placed.top).toBe(700);
    expect(placed.maxHeight).toBe(900 - 700 - 12);
  });

  it("flips to the leading side when the trailing side has no room", () => {
    const placed = placeFlyout({ ...BASE, viewportWidth: 900 });
    expect(placed.side).toBe("leading");
    expect(placed.left).toBe(124);
  });

  it("stays trailing when neither side fits, rather than flipping into the same problem", () => {
    const placed = placeFlyout({ ...BASE, cardLeft: 20, viewportWidth: 400 });
    expect(placed.side).toBe("trailing");
    expect(placed.left).toBeGreaterThanOrEqual(12);
  });

  it("never places the box off the left edge", () => {
    const placed = placeFlyout({ ...BASE, cardLeft: -200, viewportWidth: 300 });
    expect(placed.left).toBeGreaterThanOrEqual(12);
  });

  it("never places the box above the viewport", () => {
    const placed = placeFlyout({ ...BASE, rowTop: -50 });
    expect(placed.top).toBe(12);
  });
});
