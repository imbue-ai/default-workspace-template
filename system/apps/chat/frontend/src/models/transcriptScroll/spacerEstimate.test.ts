import { describe, expect, it } from "vitest";
import { computeObservedPxPerEvent, computeSpacerUpdate } from "./spacerEstimate";

describe("computeObservedPxPerEvent", () => {
  it("is the measured physical average", () => {
    expect(computeObservedPxPerEvent(5000, 100)).toBe(50);
  });

  it("is null with no loaded events or no height", () => {
    expect(computeObservedPxPerEvent(5000, 0)).toBe(null);
    expect(computeObservedPxPerEvent(0, 100)).toBe(null);
  });
});

describe("computeSpacerUpdate", () => {
  it("eases the estimate toward the observation by the smoothing factor", () => {
    const update = computeSpacerUpdate({
      previousEstimatePxPerEvent: 160,
      observedPxPerEvent: 60,
      smoothingAlpha: 0.2,
      olderUnloadedCount: 100,
      newerUnloadedCount: 50,
      previousSpacerTopPx: 16_000,
    });
    expect(update.estimatePxPerEvent).toBeCloseTo(140, 10);
    expect(update.spacerTopPx).toBe(14_000);
    expect(update.spacerBottomPx).toBe(7000);
  });

  it("reports the top-spacer change as the compensating scrollTop delta", () => {
    const update = computeSpacerUpdate({
      previousEstimatePxPerEvent: 160,
      observedPxPerEvent: 60,
      smoothingAlpha: 0.2,
      olderUnloadedCount: 100,
      newerUnloadedCount: 50,
      previousSpacerTopPx: 16_000,
    });
    expect(update.scrollTopDeltaPx).toBe(update.spacerTopPx - 16_000);
  });

  it("keeps the estimate unchanged when there is no observation", () => {
    const update = computeSpacerUpdate({
      previousEstimatePxPerEvent: 120,
      observedPxPerEvent: null,
      smoothingAlpha: 0.2,
      olderUnloadedCount: 10,
      newerUnloadedCount: 0,
      previousSpacerTopPx: 1200,
    });
    expect(update.estimatePxPerEvent).toBe(120);
    expect(update.spacerTopPx).toBe(1200);
    expect(update.scrollTopDeltaPx).toBe(0);
  });

  it("collapses both spacers to zero when everything is loaded", () => {
    const update = computeSpacerUpdate({
      previousEstimatePxPerEvent: 160,
      observedPxPerEvent: 100,
      smoothingAlpha: 1,
      olderUnloadedCount: 0,
      newerUnloadedCount: 0,
      previousSpacerTopPx: 800,
    });
    expect(update.spacerTopPx).toBe(0);
    expect(update.spacerBottomPx).toBe(0);
    expect(update.scrollTopDeltaPx).toBe(-800);
  });
});
