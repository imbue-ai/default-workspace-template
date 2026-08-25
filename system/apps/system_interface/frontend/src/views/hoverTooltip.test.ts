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

  it('defaults to the same result as an explicit "below" placement', () => {
    const anchor = { left: 400, top: 300, bottom: 320, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT)).toEqual(placeTooltip(anchor, BUBBLE, VIEWPORT, "below"));
  });
});

describe("placeTooltip right placement", () => {
  it("sits beside the trigger with a 6px gap, vertically centered on it", () => {
    const anchor = { left: 400, top: 300, bottom: 320, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT, "right")).toEqual({ left: 446, top: 300 });
  });

  it("flips to the trigger's left when the bubble would overflow the right edge", () => {
    const anchor = { left: 950, top: 300, bottom: 320, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT, "right")).toEqual({ left: 844, top: 300 });
  });

  it("never runs off the right edge even when the left flip has nowhere to go either", () => {
    // A bubble wide enough that neither the trigger's right nor its left has
    // room: the right-hand position wins (as the primary side), but the
    // right-edge clamp still pulls it back the last two pixels so the bubble
    // ends flush with the margin instead of hanging off the viewport.
    const anchor = { left: 50, top: 300, bottom: 320, width: 40 };
    expect(placeTooltip(anchor, { width: 900, height: 20 }, VIEWPORT, "right")).toEqual({ left: 94, top: 300 });
  });

  it("clamps to 6px from the top for a trigger scrolled off the top", () => {
    const anchor = { left: 400, top: -10, bottom: 10, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT, "right")).toEqual({ left: 446, top: 6 });
  });

  it("clamps to the bottom margin for a trigger against the bottom edge", () => {
    // Unlike the "below" placement's flip axis, right-placement clamps its
    // perpendicular (vertical) axis on both sides, so a low trigger cannot
    // push the bubble past the viewport's bottom edge either.
    const anchor = { left: 400, top: 790, bottom: 810, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT, "right")).toEqual({ left: 446, top: 774 });
  });
});
