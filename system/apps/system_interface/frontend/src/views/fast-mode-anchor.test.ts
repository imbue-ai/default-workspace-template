import { describe, expect, it } from "vitest";

import { computeAnchoredPosition, FAST_MODE_MODAL_WIDTH_PX } from "./fast-mode-anchor";

// A tall window with the composer toggle near its bottom edge, which is where
// the toggle sits in a real chat panel.
const VIEWPORT = { width: 1400, height: 900 };
const TOGGLE = { left: 700, top: 840, width: 32 };

describe("anchoring the fast-mode prompt to the composer toggle", () => {
  it("centers the popover over the toggle and points its arrow at it", () => {
    const position = computeAnchoredPosition(TOGGLE, VIEWPORT);

    expect(position).not.toBeNull();
    expect(position!.width).toBe(FAST_MODE_MODAL_WIDTH_PX);
    // Centered on the toggle's center (716), so the arrow lands mid-popover.
    expect(position!.left).toBe(716 - FAST_MODE_MODAL_WIDTH_PX / 2);
    expect(position!.arrowLeft).toBe(FAST_MODE_MODAL_WIDTH_PX / 2);
    // Above the toggle rather than over it.
    expect(position!.bottom).toBeGreaterThan(VIEWPORT.height - TOGGLE.top);
  });

  it("keeps the popover on screen when the toggle is near an edge, and follows it with the arrow", () => {
    const position = computeAnchoredPosition({ ...TOGGLE, left: 4 }, VIEWPORT);

    expect(position).not.toBeNull();
    // Clamped to the viewport margin instead of hanging off the left edge...
    expect(position!.left).toBeGreaterThanOrEqual(0);
    expect(position!.left).toBeLessThan(20);
    // ...with the arrow moved to stay over the toggle, and off the corner.
    expect(position!.arrowLeft).toBeGreaterThanOrEqual(20);
    expect(position!.arrowLeft).toBeLessThan(FAST_MODE_MODAL_WIDTH_PX / 2);
  });

  it("gives up and centers the modal when the toggle is unusable as an anchor", () => {
    // A composer in a hidden panel measures as a zero-size rect at the origin.
    expect(computeAnchoredPosition({ left: 0, top: 0, width: 0 }, VIEWPORT)).toBeNull();
    // A window too short to fit the popover above the toggle.
    expect(computeAnchoredPosition({ ...TOGGLE, top: 280 }, { width: 1400, height: 320 })).toBeNull();
    // A toggle scrolled out of view below the fold.
    expect(computeAnchoredPosition({ ...TOGGLE, top: 1200 }, VIEWPORT)).toBeNull();
  });
});
