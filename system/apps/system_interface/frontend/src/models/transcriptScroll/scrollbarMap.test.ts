import { describe, expect, it } from "vitest";
import { computeLiveMapping, computeThumb, resolveTrackFraction } from "./scrollbarMap";
import type { ScrollbarMapping, Viewport } from "./types";

function viewport(scrollTopPx: number, overrides: Partial<Viewport> = {}): Viewport {
  return { scrollTopPx, heightPx: 500, spacerTopPx: 0, spacerBottomPx: 0, ...overrides };
}

describe("computeLiveMapping", () => {
  it("splits the track by event-count share around the loaded window", () => {
    // Events 200..300 of 1000 loaded: the physical band is 20%..30% of the track.
    const mapping = computeLiveMapping({ totalEvents: 1000 }, { firstIndex: 200, endIndex: 300 }, 5000);
    expect(mapping.segments).toEqual([
      { kind: "virtual", trackStart: 0, trackEnd: 0.2, firstIndex: 0, endIndex: 200 },
      { kind: "physical", trackStart: 0.2, trackEnd: 0.3, heightPx: 5000 },
      { kind: "virtual", trackStart: 0.3, trackEnd: 1, firstIndex: 300, endIndex: 1000 },
    ]);
  });

  it("is a single physical segment when the whole chat is loaded", () => {
    const mapping = computeLiveMapping({ totalEvents: 100 }, { firstIndex: 0, endIndex: 100 }, 9000);
    expect(mapping.segments).toEqual([{ kind: "physical", trackStart: 0, trackEnd: 1, heightPx: 9000 }]);
  });

  it("drops the top virtual segment when loaded from the start", () => {
    const mapping = computeLiveMapping({ totalEvents: 100 }, { firstIndex: 0, endIndex: 40 }, 1000);
    expect(mapping.segments.map((s) => s.kind)).toEqual(["physical", "virtual"]);
    expect(mapping.segments[0].trackStart).toBe(0);
    expect(mapping.segments[0].trackEnd).toBeCloseTo(0.4, 10);
  });

  it("is a single virtual segment when nothing is loaded yet", () => {
    const mapping = computeLiveMapping({ totalEvents: 100 }, { firstIndex: 0, endIndex: 0 }, 0);
    expect(mapping.segments).toEqual([{ kind: "virtual", trackStart: 0, trackEnd: 1, firstIndex: 0, endIndex: 100 }]);
  });

  it("is a single physical segment for an empty chat", () => {
    const mapping = computeLiveMapping({ totalEvents: 0 }, { firstIndex: 0, endIndex: 0 }, 0);
    expect(mapping.segments).toEqual([{ kind: "physical", trackStart: 0, trackEnd: 1, heightPx: 0 }]);
  });
});

describe("resolveTrackFraction", () => {
  const mapping = computeLiveMapping({ totalEvents: 1000 }, { firstIndex: 300, endIndex: 700 }, 15_000);

  it("resolves a virtual-region position to the proportional event index", () => {
    // 15% of the track is halfway through the top virtual region (0..300).
    expect(resolveTrackFraction(mapping, 0.15)).toEqual({ kind: "virtual-index", index: 150 });
  });

  it("resolves a physical-region position to exact content pixels", () => {
    // The physical band is 30%..70%; halfway through it is half the content height.
    const target = resolveTrackFraction(mapping, 0.5);
    expect(target.kind).toBe("physical-px");
    expect(target.kind === "physical-px" && target.contentTopPx).toBeCloseTo(7500, 6);
  });

  it("maps 70% of an all-virtual track to the event 70% of the way through", () => {
    const unloaded = computeLiveMapping({ totalEvents: 1000 }, { firstIndex: 0, endIndex: 0 }, 0);
    expect(resolveTrackFraction(unloaded, 0.7)).toEqual({ kind: "virtual-index", index: 700 });
  });

  it("is continuous at segment boundaries", () => {
    const atPhysicalStart = resolveTrackFraction(mapping, 0.3);
    expect(atPhysicalStart).toEqual({ kind: "physical-px", contentTopPx: 0 });
    const justBelow = resolveTrackFraction(mapping, 0.2999);
    expect(justBelow.kind).toBe("virtual-index");
  });

  it("clamps out-of-range fractions to the track ends", () => {
    expect(resolveTrackFraction(mapping, -0.5)).toEqual({ kind: "virtual-index", index: 0 });
    const atEnd = resolveTrackFraction(mapping, 1.5);
    expect(atEnd).toEqual({ kind: "virtual-index", index: 999 });
  });
});

describe("computeThumb", () => {
  it("positions the thumb by pixel fraction within the physical band (spec example)", () => {
    // Messages 300..700 of 1000 loaded; 300..500 measure 5000px and 500..700
    // measure 10000px. A viewport at the top of message 500 sits 1/3 of the way
    // through the physical pixels, so the thumb starts at 30% + (1/3) * 40% ~= 43.3%.
    const mapping = computeLiveMapping({ totalEvents: 1000 }, { firstIndex: 300, endIndex: 700 }, 15_000);
    const spacerTopPx = 300 * 160;
    const thumb = computeThumb(
      mapping,
      viewport(spacerTopPx + 5000, { spacerTopPx, spacerBottomPx: 300 * 160 }),
      15_000,
    );
    expect(thumb.startFraction).toBeCloseTo(0.3 + (1 / 3) * 0.4, 6);
  });

  it("sizes the thumb by the viewport's share of the physical pixels", () => {
    const mapping = computeLiveMapping({ totalEvents: 1000 }, { firstIndex: 300, endIndex: 700 }, 15_000);
    const spacerTopPx = 300 * 160;
    const thumb = computeThumb(
      mapping,
      viewport(spacerTopPx + 5000, { heightPx: 1500, spacerTopPx, spacerBottomPx: 300 * 160 }),
      15_000,
    );
    expect(thumb.sizeFraction).toBeCloseTo((1500 / 15_000) * 0.4, 6);
  });

  it("positions the thumb inside a virtual band while the viewport is over a spacer", () => {
    const mapping = computeLiveMapping({ totalEvents: 1000 }, { firstIndex: 300, endIndex: 700 }, 15_000);
    const spacerTopPx = 300 * 160;
    // Halfway down the top spacer -> halfway through the 0..30% band.
    const thumb = computeThumb(mapping, viewport(spacerTopPx / 2, { spacerTopPx, spacerBottomPx: 300 * 160 }), 15_000);
    expect(thumb.startFraction).toBeCloseTo(0.15, 6);
  });

  it("spans the whole track when everything is loaded and visible", () => {
    const mapping = computeLiveMapping({ totalEvents: 10 }, { firstIndex: 0, endIndex: 10 }, 400);
    const thumb = computeThumb(mapping, viewport(0, { heightPx: 400 }), 400);
    expect(thumb.startFraction).toBe(0);
    expect(thumb.sizeFraction).toBeCloseTo(1, 6);
  });

  it("reaches the end of the track at the bottom of the content", () => {
    const mapping = computeLiveMapping({ totalEvents: 10 }, { firstIndex: 0, endIndex: 10 }, 2000);
    const thumb = computeThumb(mapping, viewport(1500, { heightPx: 500 }), 2000);
    expect(thumb.startFraction + thumb.sizeFraction).toBeCloseTo(1, 6);
  });
});

describe("frozen-mapping scrubbing (SCROLLBAR state)", () => {
  it("keeps resolving through the frozen mapping regardless of newer loads", () => {
    const frozen: ScrollbarMapping = computeLiveMapping(
      { totalEvents: 1000 },
      { firstIndex: 300, endIndex: 700 },
      15_000,
    );
    // The physical window has since moved, but scrubbing still uses `frozen`:
    // 75% of the track resolves through the frozen bottom virtual band (700..1000).
    const target = resolveTrackFraction(frozen, 0.75);
    expect(target).toEqual({ kind: "virtual-index", index: 750 });
  });
});
