import { describe, expect, it } from "vitest";
import {
  COLD_RESERVE_RATE,
  RESERVE_RATE_CHANGE_THRESHOLD,
  RESERVE_RATE_SAMPLE_ROWS,
  nextReserveRate,
  type ReserveRateSample,
} from "./reserveRate";
import { DEFAULT_EVENT_HEIGHT_PX } from "./rowGeometry";

/** `count` identical rows, each covering `events` events at `height` pixels. */
function rows(count: number, events: number, height: number, is_measured = true): ReserveRateSample[] {
  return Array.from({ length: count }, () => ({ events, height, is_measured }));
}

describe("nextReserveRate", () => {
  it("starts a transcript at the cold default", () => {
    expect(COLD_RESERVE_RATE).toEqual({ rate: DEFAULT_EVENT_HEIGHT_PX, is_settled: false });
  });

  it("replaces the cold default with what the transcript actually renders at", () => {
    // The regression this exists for: a tool-heavy turn is one row of 340px over
    // 50 events, nowhere near the 160px/event the cold default assumes.
    const next = nextReserveRate(rows(4, 50, 340), COLD_RESERVE_RATE);

    expect(next.rate).toBeCloseTo(340 / 50);
  });

  it("weights by events across the whole window rather than averaging rows", () => {
    // One 50-event turn at 340px and one 1-event bubble at 90px is 430px over 51
    // events -- not the mean of the two rows' own per-event rates, which the tall
    // turn would barely register in.
    const next = nextReserveRate(
      [
        { events: 50, height: 340, is_measured: true },
        { events: 1, height: 90, is_measured: true },
      ],
      COLD_RESERVE_RATE,
    );

    expect(next.rate).toBeCloseTo(430 / 51);
  });

  it("counts a row that has not been measured yet at its estimate", () => {
    // Taking the rate over only the measured rows made it lurch as different
    // kinds of row settled at different times, moving the reserve by thousands of
    // pixels seconds after load.
    const measuredOnly = nextReserveRate([{ events: 1, height: 20, is_measured: true }], COLD_RESERVE_RATE);
    const withAnEstimate = nextReserveRate(
      [
        { events: 1, height: 20, is_measured: true },
        { events: 1, height: 400, is_measured: false },
      ],
      COLD_RESERVE_RATE,
    );

    expect(measuredOnly.rate).toBeCloseTo(20);
    expect(withAnEstimate.rate).toBeCloseTo(210);
  });

  it("ignores a row that covers no events", () => {
    // Two rows resolving to the same offset leave the first covering nothing. It
    // says nothing about a rate per event, and dividing by its zero events would
    // corrupt the answer for the rest.
    const withoutIt = nextReserveRate(rows(2, 10, 500), COLD_RESERVE_RATE);
    const withIt = nextReserveRate([...rows(2, 10, 500), { events: 0, height: 90, is_measured: true }], {
      ...COLD_RESERVE_RATE,
    });

    expect(withIt.rate).toBe(withoutIt.rate);
  });

  it("leaves a converged rate alone for movement within the threshold", () => {
    // Once converged, ordinary measurement noise must not move it, or the scroll
    // height churns and the scrollbar crawls.
    const converged = { rate: 100, is_settled: false };
    const noise = converged.rate * (1 + RESERVE_RATE_CHANGE_THRESHOLD / 2);

    expect(nextReserveRate(rows(1, 1, noise), converged).rate).toBe(converged.rate);
  });

  it("adopts a rate that has moved past the threshold", () => {
    // The cold default meeting reality, or a conversation whose character shifts.
    const converged = { rate: 100, is_settled: false };
    const moved = converged.rate * (1 + RESERVE_RATE_CHANGE_THRESHOLD * 2);

    expect(nextReserveRate(rows(1, 1, moved), converged).rate).toBe(moved);
  });

  it("fixes the rate once enough rows have been measured", () => {
    const nearly = nextReserveRate(rows(RESERVE_RATE_SAMPLE_ROWS - 1, 1, 100), COLD_RESERVE_RATE);
    const enough = nextReserveRate(rows(RESERVE_RATE_SAMPLE_ROWS, 1, 100), COLD_RESERVE_RATE);

    expect(nearly.is_settled).toBe(false);
    expect(enough.is_settled).toBe(true);
  });

  it("does not count an unmeasured row towards settling", () => {
    // The sample is about how much the client has actually seen; a window full of
    // estimates has seen nothing.
    const next = nextReserveRate(rows(RESERVE_RATE_SAMPLE_ROWS * 2, 1, 100, false), COLD_RESERVE_RATE);

    expect(next.is_settled).toBe(false);
  });

  it("never moves a settled rate again", () => {
    // Every later refinement would resize the scroll container under a reader,
    // and a shrink that pulls the bottom up to the viewport re-arms tail-following
    // underneath them.
    const settled = { rate: 34, is_settled: true };

    expect(nextReserveRate(rows(50, 1, 5000), settled)).toBe(settled);
  });

  it("neither moves nor settles on a window that offers no evidence", () => {
    // A rate divided out of nothing measures nothing, so an empty window -- or one
    // whose rows all cover no events -- must leave the answer exactly as it was.
    const converged = { rate: 100, is_settled: false };

    expect(nextReserveRate([], converged)).toBe(converged);
    expect(nextReserveRate(rows(RESERVE_RATE_SAMPLE_ROWS * 2, 0, 100), converged)).toBe(converged);
  });

  it("ignores a window whose rows are all laid out at zero height", () => {
    const converged = { rate: 100, is_settled: false };

    expect(nextReserveRate(rows(RESERVE_RATE_SAMPLE_ROWS * 2, 1, 0), converged)).toBe(converged);
  });
});
