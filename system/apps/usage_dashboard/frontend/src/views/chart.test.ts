import { describe, expect, it } from "vitest";

import { markerPosition } from "./chart";

describe("markerPosition", () => {
  const starts = [0, 1, 2, 3].map((hour) => new Date(Date.UTC(2026, 9, 22, hour)));
  const now = new Date(Date.UTC(2026, 9, 22, 3, 30));

  it("places a moment within its bar, as a share of the chart's width", () => {
    expect(markerPosition(starts, now, new Date(Date.UTC(2026, 9, 22, 1, 30)))).toBeCloseTo(1.5 / 4);
    expect(markerPosition(starts, now, starts[0])).toBe(0);
  });

  it("reads the last bar as running up to now, and leaves out moments outside the bars", () => {
    expect(markerPosition(starts, now, new Date(Date.UTC(2026, 9, 22, 3, 15)))).toBeCloseTo(3.5 / 4);
    expect(markerPosition(starts, now, new Date(Date.UTC(2026, 9, 21, 23)))).toBeNull();
    expect(markerPosition(starts, now, new Date(Date.UTC(2026, 9, 22, 4)))).toBeNull();
  });
});
