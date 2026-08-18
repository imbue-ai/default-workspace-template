import { describe, expect, it } from "vitest";

import { placeTooltip } from "./hoverTooltip";

const VIEWPORT = { width: 1000, height: 800 };
const BUBBLE = { width: 100, height: 20 };

describe("placeTooltip", () => {
  it("centers the bubble under the trigger with a 6px gap", () => {
    const anchor = { left: 400, top: 300, bottom: 320, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT)).toEqual({ left: 370, top: 326 });
  });

  it("flips above the trigger when the bubble would overflow the bottom", () => {
    const anchor = { left: 400, top: 760, bottom: 790, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT)).toEqual({ left: 370, top: 734 });
  });

  it("stays below when flipping above would not fit either", () => {
    // A trigger taller than the viewport: neither side has room, so the bubble
    // keeps its natural place under the trigger and only the edge clamp applies.
    const anchor = { left: 400, top: 2, bottom: 795, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT)).toEqual({ left: 370, top: 801 });
  });

  it("clamps to 6px from the left edge for a trigger against it", () => {
    const anchor = { left: 0, top: 300, bottom: 320, width: 20 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT).left).toBe(6);
  });

  it("clamps to 6px from the right edge for a trigger against it", () => {
    const anchor = { left: 980, top: 300, bottom: 320, width: 20 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT).left).toBe(894);
  });

  it("clamps to 6px from the top for a trigger scrolled off the top", () => {
    const anchor = { left: 400, top: -60, bottom: -30, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT).top).toBe(6);
  });

  it("prefers the left clamp when the bubble is wider than the viewport", () => {
    const anchor = { left: 400, top: 300, bottom: 320, width: 40 };
    expect(placeTooltip(anchor, { width: 1200, height: 20 }, VIEWPORT).left).toBe(6);
  });
});
