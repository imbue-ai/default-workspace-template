import { describe, expect, it } from "vitest";

import { placeFlyout } from "./flyout-position";

const BASE = {
  parent: { left: 400, right: 700 },
  rowTop: 300,
  flyoutWidth: 280,
  maxFlyoutHeight: 420,
  viewportWidth: 1400,
  viewportHeight: 900,
  margin: 12,
  overlap: 4,
};

describe("placeFlyout", () => {
  it("tucks under the parent's trailing edge and top-aligns with the row", () => {
    const placed = placeFlyout(BASE);
    expect(placed).toMatchObject({ left: 696, top: 300, side: "trailing" });
  });

  it("flips to the leading side when the trailing side has no room", () => {
    const placed = placeFlyout({ ...BASE, viewportWidth: 900 });
    expect(placed.side).toBe("leading");
    expect(placed.left).toBe(124);
  });

  it("stays trailing when neither side fits, rather than flipping into the same problem", () => {
    const placed = placeFlyout({ ...BASE, parent: { left: 20, right: 320 }, viewportWidth: 400 });
    expect(placed.side).toBe("trailing");
    expect(placed.left).toBeGreaterThanOrEqual(12);
  });

  it("lifts a flyout opened from a row near the bottom instead of leaving a sliver", () => {
    const placed = placeFlyout({ ...BASE, rowTop: 850 });
    expect(placed.top).toBeLessThan(850);
    expect(placed.top + placed.maxHeight).toBeLessThanOrEqual(900 - 12);
  });

  it("caps the height to the space below, never past the viewport", () => {
    const placed = placeFlyout({ ...BASE, rowTop: 600, viewportHeight: 700 });
    expect(placed.top + placed.maxHeight).toBeLessThanOrEqual(700 - 12);
  });

  it("never places the box off the left edge", () => {
    const placed = placeFlyout({ ...BASE, parent: { left: -200, right: -50 }, viewportWidth: 300 });
    expect(placed.left).toBeGreaterThanOrEqual(12);
  });
});
