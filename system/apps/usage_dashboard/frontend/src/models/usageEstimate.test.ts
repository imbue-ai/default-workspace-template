import { describe, expect, it } from "vitest";

import type { LimitWindow } from "./providers";
import { totalsRow } from "../testing/rows";
import {
  calibrationSpan,
  chosenScale,
  formatShare,
  limitShares,
  limitWindowStart,
  lineKey,
  percentPerDollar,
} from "./usageEstimate";

function window(patch: Partial<LimitWindow>): LimitWindow {
  return {
    key: "seven_day",
    label: "Weekly limit",
    usedPercent: 34,
    resetsAt: new Date("2026-10-27T00:00:00Z"),
    isResetSinceReading: false,
    level: "ok",
    windowSeconds: 604_800,
    observedAt: new Date("2026-10-22T20:00:00Z"),
    readingPercent: 34,
    readingResetsAt: new Date("2026-10-27T00:00:00Z"),
    firstPercent: 30,
    firstObservedAt: new Date("2026-10-22T12:00:00Z"),
    ...patch,
  };
}

describe("calibrationSpan", () => {
  it("runs from just after the window's first reading to just after its latest, over the share it rose", () => {
    expect(calibrationSpan(window({}))).toEqual({
      since: new Date("2026-10-22T12:00:00.001Z"),
      until: new Date("2026-10-22T20:00:00.001Z"),
      rise: 4,
    });
  });

  it("leaves out the first reading's call and keeps the latest one's, as each reading already counts its own", () => {
    // The collector dates a reading to its call's end (here to the microsecond, which a Date truncates) and keeps
    // calls that ended in [since, until).
    const firstCall = Date.parse("2026-10-22T12:00:00.250Z") + 0.4;
    const latestCall = Date.parse("2026-10-22T20:00:00.130Z") + 0.649;
    const span = calibrationSpan(
      window({
        firstObservedAt: new Date("2026-10-22T12:00:00.250400Z"),
        observedAt: new Date("2026-10-22T20:00:00.130649Z"),
      }),
    );

    expect(span).not.toBeNull();
    expect(firstCall).toBeLessThan(span!.since.getTime());
    expect(latestCall).toBeLessThan(span!.until.getTime());
  });

  it("keeps the last reading's span once its window has reset", () => {
    const reset = window({ isResetSinceReading: true, usedPercent: 0, resetsAt: null });

    expect(calibrationSpan(reset)?.rise).toBe(4);
  });

  it("has nothing to measure until the window has risen, or without a first reading", () => {
    expect(calibrationSpan(window({ firstPercent: 34 }))).toBeNull();
    expect(calibrationSpan(window({ firstPercent: null, firstObservedAt: null }))).toBeNull();
    expect(calibrationSpan(window({ firstObservedAt: new Date("2026-10-22T20:00:00Z") }))).toBeNull();
  });
});

describe("limitWindowStart", () => {
  it("is the reading's reset less the window's length", () => {
    expect(limitWindowStart(window({}))).toEqual(new Date("2026-10-20T00:00:00Z"));
    expect(limitWindowStart(window({ windowSeconds: null }))).toBeNull();
  });
});

describe("percentPerDollar", () => {
  it("divides the window's rise by the list price of the calls it rose across", () => {
    expect(percentPerDollar(4, 20)).toBeCloseTo(0.2);
    expect(percentPerDollar(4, 0)).toBeNull();
  });
});

describe("chosenScale and formatShare", () => {
  const at = { isFromEarlierWindow: false, windowStart: new Date(0), resetsAt: new Date(0) };
  const scales = [
    { windowKey: "five_hour", label: "5-hour limit", percentPerDollar: 2, ...at },
    { windowKey: "seven_day", label: "Weekly limit", percentPerDollar: 0.4, ...at },
  ];

  it("picks the viewer's window, else the first", () => {
    expect(chosenScale(scales, "seven_day")?.percentPerDollar).toBe(0.4);
    expect(chosenScale(scales, undefined)?.windowKey).toBe("five_hour");
    expect(chosenScale([], undefined)).toBeUndefined();
  });

  it("writes small shares with enough digits to tell calls apart", () => {
    expect(formatShare(0.004)).toBe("<0.01%");
    expect(formatShare(0.4213)).toBe("0.42%");
    expect(formatShare(3.14)).toBe("3.1%");
  });
});

describe("limitShares", () => {
  it("prices each line's subscription calls in the window at the scale's rate", () => {
    const scale = {
      windowKey: "five_hour",
      label: "5-hour limit",
      percentPerDollar: 10,
      isFromEarlierWindow: false,
      windowStart: new Date(0),
      resetsAt: new Date(0),
    };
    const rows = [
      totalsRow({ group: { model: "anthropic/opus", billing_mode: "SUBSCRIPTION" }, cost_usd: "2" }),
      totalsRow({ group: { model: "anthropic/haiku", billing_mode: "SUBSCRIPTION" }, cost_usd: "0.5" }),
      totalsRow({ group: { model: "anthropic/sonnet", billing_mode: "SUBSCRIPTION" }, cost_usd: null }),
    ];

    const shares = limitShares("model", rows, scale);

    expect(shares.get(lineKey({ model: "anthropic/opus" }))).toBe(20);
    expect(shares.get(lineKey({ model: "anthropic/haiku" }))).toBe(5);
    expect(shares.has(lineKey({ model: "anthropic/sonnet" }))).toBe(false);
  });
});
