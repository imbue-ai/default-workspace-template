import { describe, expect, it } from "vitest";
import { computeVisibleWindow } from "./virtualWindow";

const uniform =
  (height: number) =>
  (_index: number): number =>
    height;

describe("computeVisibleWindow", () => {
  it("returns an empty window with no padding for an empty list", () => {
    const result = computeVisibleWindow({
      count: 0,
      getHeight: uniform(100),
      scrollTop: 0,
      viewportHeight: 500,
      overscanPx: 0,
    });
    expect(result).toEqual({ startIndex: 0, endIndex: 0, topPad: 0, bottomPad: 0, totalHeight: 0 });
  });

  it("renders only the viewport slice with spacers summing to the total height", () => {
    // 100 rows of 100px = 10000px tall; viewport 500px at the top, no overscan.
    const result = computeVisibleWindow({
      count: 100,
      getHeight: uniform(100),
      scrollTop: 0,
      viewportHeight: 500,
      overscanPx: 0,
    });
    expect(result.startIndex).toBe(0);
    // rows 0..4 fully cover 0..500; row 5 starts exactly at 500 (not < 500).
    expect(result.endIndex).toBe(5);
    expect(result.topPad).toBe(0);
    expect(result.bottomPad).toBe(100 * 100 - 500);
    expect(result.totalHeight).toBe(10000);
    // Spacers + rendered rows always reconstruct the full height.
    const renderedHeight = (result.endIndex - result.startIndex) * 100;
    expect(result.topPad + renderedHeight + result.bottomPad).toBe(result.totalHeight);
  });

  it("windows around a mid-list scroll position", () => {
    const result = computeVisibleWindow({
      count: 100,
      getHeight: uniform(100),
      scrollTop: 5000,
      viewportHeight: 500,
      overscanPx: 0,
    });
    // Viewport covers 5000..5500 -> rows 50..54.
    expect(result.startIndex).toBe(50);
    expect(result.endIndex).toBe(55);
    expect(result.topPad).toBe(5000);
    expect(result.bottomPad).toBe(10000 - 5500);
  });

  it("expands the window by the overscan margin on both sides", () => {
    const noOverscan = computeVisibleWindow({
      count: 100,
      getHeight: uniform(100),
      scrollTop: 5000,
      viewportHeight: 500,
      overscanPx: 0,
    });
    const withOverscan = computeVisibleWindow({
      count: 100,
      getHeight: uniform(100),
      scrollTop: 5000,
      viewportHeight: 500,
      overscanPx: 200,
    });
    expect(withOverscan.startIndex).toBeLessThan(noOverscan.startIndex);
    expect(withOverscan.endIndex).toBeGreaterThan(noOverscan.endIndex);
  });

  it("handles variable row heights", () => {
    // Heights: row i is (i + 1) * 10 px. Cumulative offset of row k is
    // 10 * (1 + 2 + ... + k) = 5 * k * (k + 1).
    const heights = (i: number) => (i + 1) * 10;
    const result = computeVisibleWindow({
      count: 20,
      getHeight: heights,
      scrollTop: 100,
      viewportHeight: 50,
      overscanPx: 0,
    });
    // Reconstruct total and verify the pads bracket the rendered rows exactly.
    let total = 0;
    for (let i = 0; i < 20; i++) total += heights(i);
    let rendered = 0;
    for (let i = result.startIndex; i < result.endIndex; i++) rendered += heights(i);
    expect(result.topPad + rendered + result.bottomPad).toBe(total);
    expect(result.totalHeight).toBe(total);
    // The first rendered row must straddle or follow scrollTop=100; the row
    // before it must end at or before 100.
    let offsetBeforeStart = 0;
    for (let i = 0; i < result.startIndex; i++) offsetBeforeStart += heights(i);
    expect(offsetBeforeStart).toBeLessThanOrEqual(100);
  });

  it("fills backward to cover the viewport when scrolled past the end", () => {
    const result = computeVisibleWindow({
      count: 10,
      getHeight: uniform(100),
      scrollTop: 100000,
      viewportHeight: 500,
      overscanPx: 0,
    });
    // Coverage = viewport (500) + 2*overscan (0) = 500px -> the last 5 rows.
    expect(result.startIndex).toBe(5);
    expect(result.endIndex).toBe(10);
    expect(result.bottomPad).toBe(0);
    expect(result.topPad).toBe(500);
    const rendered = (result.endIndex - result.startIndex) * 100;
    expect(result.topPad + rendered + result.bottomPad).toBe(result.totalHeight);
  });

  it("includes overscan in the past-the-end backward fill", () => {
    const result = computeVisibleWindow({
      count: 10,
      getHeight: uniform(100),
      scrollTop: 100000,
      viewportHeight: 500,
      overscanPx: 200,
    });
    // Coverage = 500 + 2*200 = 900px -> the last 9 rows.
    expect(result.startIndex).toBe(1);
    expect(result.endIndex).toBe(10);
    expect(result.bottomPad).toBe(0);
  });

  it("expands the window downward to keep a pinned row below the viewport mounted", () => {
    const result = computeVisibleWindow({
      count: 100,
      getHeight: uniform(100),
      scrollTop: 0,
      viewportHeight: 500,
      overscanPx: 0,
      pinnedRange: { start: 80, end: 82 },
    });
    // Viewport alone would render rows 0..4; the pin drags the end out to 83.
    expect(result.startIndex).toBe(0);
    expect(result.endIndex).toBe(83);
    expect(result.topPad).toBe(0);
    expect(result.bottomPad).toBe((100 - 83) * 100);
  });

  it("expands the window upward to keep a pinned row above the viewport mounted", () => {
    const result = computeVisibleWindow({
      count: 100,
      getHeight: uniform(100),
      scrollTop: 5000,
      viewportHeight: 500,
      overscanPx: 0,
      pinnedRange: { start: 10, end: 10 },
    });
    // Viewport alone would render rows 50..54; the pin drags the start up to 10.
    expect(result.startIndex).toBe(10);
    expect(result.endIndex).toBe(55);
    expect(result.topPad).toBe(10 * 100);
  });

  it("leaves the window unchanged when the pinned range is already inside it", () => {
    const withPin = computeVisibleWindow({
      count: 100,
      getHeight: uniform(100),
      scrollTop: 5000,
      viewportHeight: 500,
      overscanPx: 0,
      pinnedRange: { start: 51, end: 52 },
    });
    expect(withPin.startIndex).toBe(50);
    expect(withPin.endIndex).toBe(55);
  });

  it("clamps an out-of-range pinned range instead of producing a bad slice", () => {
    const result = computeVisibleWindow({
      count: 10,
      getHeight: uniform(100),
      scrollTop: 0,
      viewportHeight: 200,
      overscanPx: 0,
      pinnedRange: { start: -5, end: 999 },
    });
    expect(result.startIndex).toBe(0);
    expect(result.endIndex).toBe(10);
    expect(result.topPad).toBe(0);
    expect(result.bottomPad).toBe(0);
  });

  it("keeps a pinned row mounted even past the end of the content", () => {
    const result = computeVisibleWindow({
      count: 20,
      getHeight: uniform(100),
      scrollTop: 100000,
      viewportHeight: 300,
      overscanPx: 0,
      pinnedRange: { start: 2, end: 2 },
    });
    // Past-the-end backward fill covers the tail; the pin additionally holds row 2.
    expect(result.startIndex).toBe(2);
    expect(result.endIndex).toBe(20);
    const rendered = (result.endIndex - result.startIndex) * 100;
    expect(result.topPad + rendered + result.bottomPad).toBe(result.totalHeight);
  });
});
