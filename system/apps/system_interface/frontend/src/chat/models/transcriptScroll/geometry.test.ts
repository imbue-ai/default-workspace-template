import { describe, expect, it } from "vitest";
import {
  anchorFromViewport,
  buildPhysicalGeometry,
  computeVisibleRowRange,
  rowHeightAt,
  rowIndexOfKey,
  scrollTopForAnchor,
} from "./geometry";
import type { Viewport } from "./types";

function viewportAt(scrollTopPx: number, overrides: Partial<Viewport> = {}): Viewport {
  return { scrollTopPx, heightPx: 500, spacerTopPx: 0, spacerBottomPx: 0, ...overrides };
}

// Four rows: tops at 0, 100, 350, 380 (heights 100, 250, 30, 90); total 470.
const geometry = buildPhysicalGeometry([
  { key: "a", measuredPx: 100, estimatePx: 160 },
  { key: "b", measuredPx: 250, estimatePx: 160 },
  { key: "c", measuredPx: null, estimatePx: 30 },
  { key: "d", measuredPx: 90, estimatePx: 160 },
]);

describe("buildPhysicalGeometry", () => {
  it("builds prefix sums using measurements and falling back to estimates", () => {
    expect(geometry.rowTops).toEqual([0, 100, 350, 380]);
    expect(geometry.totalHeightPx).toBe(470);
    expect(geometry.unmeasuredCount).toBe(1);
  });

  it("handles an empty row list", () => {
    const empty = buildPhysicalGeometry([]);
    expect(empty.totalHeightPx).toBe(0);
    expect(empty.rowKeys).toEqual([]);
  });

  it("looks up row indexes by key", () => {
    expect(rowIndexOfKey(geometry, "c")).toBe(2);
    expect(rowIndexOfKey(geometry, "missing")).toBe(null);
  });

  it("derives per-row heights from the prefix sums", () => {
    expect(rowHeightAt(geometry, 0)).toBe(100);
    expect(rowHeightAt(geometry, 3)).toBe(90);
  });
});

describe("anchorFromViewport", () => {
  it("anchors to the row containing the viewport top, with a negative offset into it", () => {
    // Viewport top at 120 sits inside row b (100..350): b is the top message on
    // screen, so it is the anchor -- holding it is what keeps that message put
    // when its own height corrects (measurement, expand/collapse).
    expect(anchorFromViewport(geometry, viewportAt(120))).toEqual({ rowKey: "b", offsetPx: -20 });
  });

  it("anchors with offset 0 when a row top aligns exactly with the viewport top", () => {
    expect(anchorFromViewport(geometry, viewportAt(100))).toEqual({ rowKey: "b", offsetPx: 0 });
  });

  it("accounts for the top spacer when locating the viewport in content space", () => {
    expect(anchorFromViewport(geometry, viewportAt(1100, { spacerTopPx: 1000 }))).toEqual({
      rowKey: "b",
      offsetPx: 0,
    });
  });

  it("anchors inside a tall trailing row", () => {
    expect(anchorFromViewport(geometry, viewportAt(400))).toEqual({ rowKey: "d", offsetPx: -20 });
  });

  it("anchors the first row with a positive offset while over the top spacer", () => {
    expect(anchorFromViewport(geometry, viewportAt(500, { spacerTopPx: 1000 }))).toEqual({
      rowKey: "a",
      offsetPx: 500,
    });
  });

  it("returns null for empty geometry", () => {
    expect(anchorFromViewport(buildPhysicalGeometry([]), viewportAt(0))).toBe(null);
  });
});

describe("scrollTopForAnchor", () => {
  it("round-trips exactly: re-deriving scrollTop from the anchor returns the original", () => {
    for (const spacerTopPx of [0, 777]) {
      for (const scrollTopPx of [0, 1, 99.5, 100, 119.25, 350, 400, 469, 470, 500]) {
        const viewport = viewportAt(scrollTopPx + spacerTopPx, { spacerTopPx });
        const anchor = anchorFromViewport(geometry, viewport);
        expect(anchor).not.toBe(null);
        expect(scrollTopForAnchor(geometry, anchor!, spacerTopPx)).toBeCloseTo(scrollTopPx + spacerTopPx, 6);
      }
    }
  });

  it("holds the anchored row still when heights above it change", () => {
    const anchor = anchorFromViewport(geometry, viewportAt(360))!;
    expect(anchor.rowKey).toBe("c");
    // Row b grows by 500px (e.g. a measurement landing); the anchor row's top
    // moves down by 500, and the derived scrollTop follows it exactly.
    const grown = buildPhysicalGeometry([
      { key: "a", measuredPx: 100, estimatePx: 160 },
      { key: "b", measuredPx: 750, estimatePx: 160 },
      { key: "c", measuredPx: null, estimatePx: 30 },
      { key: "d", measuredPx: 90, estimatePx: 160 },
    ]);
    expect(scrollTopForAnchor(grown, anchor, 0)).toBe(360 + 500);
  });

  it("returns null when the anchor row no longer exists", () => {
    expect(scrollTopForAnchor(geometry, { rowKey: "gone", offsetPx: 0 }, 0)).toBe(null);
  });
});

describe("computeVisibleRowRange", () => {
  it("selects the rows intersecting the viewport plus overscan", () => {
    // Window [100, 150) with no overscan: row b spans it entirely.
    const range = computeVisibleRowRange(geometry, viewportAt(100, { heightPx: 50 }), 0);
    expect(range).toEqual({ startIndex: 1, endIndex: 2 });
  });

  it("includes a row that spans the window top boundary", () => {
    // Window starts at 150, inside row b (100..350).
    const range = computeVisibleRowRange(geometry, viewportAt(150, { heightPx: 50 }), 0);
    expect(range.startIndex).toBe(1);
  });

  it("expands the range by the overscan", () => {
    // Window [50, 300): rows a and b intersect; row c starts at 350, outside.
    const range = computeVisibleRowRange(geometry, viewportAt(150, { heightPx: 50 }), 100);
    expect(range).toEqual({ startIndex: 0, endIndex: 2 });
  });

  it("runs in content space below the top spacer", () => {
    const range = computeVisibleRowRange(geometry, viewportAt(1100, { heightPx: 50, spacerTopPx: 1000 }), 0);
    expect(range).toEqual({ startIndex: 1, endIndex: 2 });
  });

  it("renders at least one row when the viewport sits inside a single tall row", () => {
    const range = computeVisibleRowRange(geometry, viewportAt(200, { heightPx: 10 }), 0);
    expect(range).toEqual({ startIndex: 1, endIndex: 2 });
  });

  it("fills backward from the end on a transient overshoot past all content", () => {
    const range = computeVisibleRowRange(geometry, viewportAt(10_000, { heightPx: 100 }), 100);
    expect(range.endIndex).toBe(4);
    expect(range.startIndex).toBeLessThan(4);
    // Enough rows to cover viewport + both overscans (300px): rows d, c, b.
    expect(range.startIndex).toBe(1);
  });

  it("returns an empty range for empty geometry", () => {
    expect(computeVisibleRowRange(buildPhysicalGeometry([]), viewportAt(0), 100)).toEqual({
      startIndex: 0,
      endIndex: 0,
    });
  });
});
