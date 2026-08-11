import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. Vitest's default (node) environment has no such global, so provide
// a polyfill before any import is evaluated.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import { equalTabWidth, isTitleTruncated } from "./DockviewWorkspace";

describe("equalTabWidth", () => {
  it("shares what is left of a strip once the '+' is accounted for", () => {
    // (644 - 44) / 4 = 150.
    expect(equalTabWidth([{ width: 644, tabCount: 4 }])).toBe(150);
  });

  it("takes the narrowest strip's ideal so no strip has to scroll", () => {
    // The 4-tab strip wants 150, the 6-tab one wants 106; everybody gets 106.
    expect(
      equalTabWidth([
        { width: 644, tabCount: 4 },
        { width: 680, tabCount: 6 },
      ]),
    ).toBe(106);
  });

  it("clamps a crowded strip up to the 100px floor", () => {
    // (400 - 44) / 12 = 29.7, far below anything readable.
    expect(equalTabWidth([{ width: 400, tabCount: 12 }])).toBe(100);
  });

  it("clamps a roomy strip down to the 220px ceiling", () => {
    expect(equalTabWidth([{ width: 1200, tabCount: 1 }])).toBe(220);
  });

  it("ignores strips that hold no tabs", () => {
    // An empty strip's ideal would be infinite and would never win anyway; a
    // strip mid-teardown must not drag the shared width to the ceiling either.
    expect(
      equalTabWidth([
        { width: 644, tabCount: 4 },
        { width: 300, tabCount: 0 },
      ]),
    ).toBe(150);
  });

  it("answers with the ceiling when there are no tabs at all", () => {
    expect(equalTabWidth([{ width: 300, tabCount: 0 }])).toBe(220);
    expect(equalTabWidth([])).toBe(220);
  });

  it("rounds to whole pixels", () => {
    // (500 - 44) / 7 = 65.14 -> clamped to the floor; (700 - 44) / 5 = 131.2.
    expect(equalTabWidth([{ width: 700, tabCount: 5 }])).toBe(131);
  });

  it("never goes negative on a strip narrower than its own reserved space", () => {
    expect(equalTabWidth([{ width: 20, tabCount: 1 }])).toBe(100);
  });
});

describe("isTitleTruncated", () => {
  it("is false for a title that fits", () => {
    expect(isTitleTruncated(80, 140)).toBe(false);
  });

  it("is true for a title wider than its box", () => {
    expect(isTitleTruncated(260, 140)).toBe(true);
  });

  it("tolerates a sub-pixel overhang so an exact fit stays crisp", () => {
    expect(isTitleTruncated(140.4, 140)).toBe(false);
    expect(isTitleTruncated(141.5, 140)).toBe(true);
  });
});
