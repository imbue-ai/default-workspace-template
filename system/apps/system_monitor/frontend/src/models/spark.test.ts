import { describe, expect, it } from "vitest";

import { nearestSample, sparkPath } from "./spark";

const BOX = { width: 100, height: 100, spanMs: 100, gapMs: 15, end: 100 };

describe("sparkPath", () => {
  it("draws a line through the samples, placed by time across the span and by value up from the bottom", () => {
    const samples = [
      { t: 0, cpu: 0, memory: null },
      { t: 50, cpu: 0.5, memory: null },
    ];
    expect(sparkPath(samples, "cpu", { ...BOX, gapMs: 100 })).toBe("M0.0 100.0 L50.0 50.0");
  });

  it("breaks the line at a gap longer than the span's gap and at an unknown reading", () => {
    const samples = [
      { t: 0, cpu: 0.1, memory: null },
      { t: 10, cpu: 0.1, memory: null },
      { t: 40, cpu: 0.2, memory: null },
      { t: 50, cpu: null, memory: null },
      { t: 60, cpu: 0.3, memory: null },
    ];
    expect(sparkPath(samples, "cpu", BOX)).toBe("M0.0 90.0 L10.0 90.0 M40.0 80.0 M60.0 70.0");
  });

  it("closes the area under each stretch of the line down to the bottom", () => {
    const samples = [
      { t: 0, cpu: 0.5, memory: null },
      { t: 10, cpu: 0.5, memory: null },
    ];
    expect(sparkPath(samples, "cpu", { ...BOX, isArea: true })).toBe(
      "M0.0 100 L0.0 50.0 L10.0 50.0 L10.0 100 L0.0 100 Z",
    );
  });
});

describe("nearestSample", () => {
  it("answers the known reading nearest the time, skipping unknown ones", () => {
    const samples = [
      { t: 0, cpu: 0.1, memory: 0.2 },
      { t: 10, cpu: null, memory: 0.3 },
      { t: 20, cpu: 0.4, memory: 0.5 },
    ];
    expect(nearestSample(samples, "cpu", 9)?.t).toBe(0);
    expect(nearestSample(samples, "memory", 9)?.t).toBe(10);
    expect(nearestSample([], "cpu", 0)).toBeNull();
  });
});
