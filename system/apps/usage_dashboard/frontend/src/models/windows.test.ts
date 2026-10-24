import { describe, expect, it } from "vitest";

import { totalsRow } from "../testing/rows";
import { barStarts, binRows, windowStart } from "./windows";

const NOW = new Date(2026, 9, 22, 14, 30);

describe("windowStart", () => {
  it("starts the month at local midnight on the 1st", () => {
    expect(windowStart("month", NOW)).toEqual(new Date(2026, 9, 1));
  });

  it("starts the week at local midnight six days back", () => {
    expect(windowStart("7d", NOW)).toEqual(new Date(2026, 9, 16));
  });

  it("rolls the short windows back from now", () => {
    expect(NOW.getTime() - windowStart("30m", NOW).getTime()).toBe(30 * 60_000);
    expect(NOW.getTime() - windowStart("24h", NOW).getTime()).toBe(24 * 3_600_000);
  });
});

describe("barStarts", () => {
  it("has a bar per day so far this month and per day of the week", () => {
    expect(barStarts("month", NOW)).toHaveLength(22);
    expect(barStarts("7d", NOW)).toHaveLength(7);
  });

  it("has a bar per minute of the last half hour", () => {
    expect(barStarts("30m", NOW)).toHaveLength(31);
  });
});

describe("binRows", () => {
  it("adds hourly buckets into local days across the end of daylight saving", () => {
    const now = new Date(2026, 10, 3, 12, 0);
    const rows = [
      // 23:00 on Oct 31 in New York (still daylight time).
      totalsRow({ bucket_start: "2026-11-01T03:00:00Z", calls: 1 }),
      // Midnight on Nov 1.
      totalsRow({ bucket_start: "2026-11-01T04:00:00Z", calls: 2 }),
      // 23:00 on Nov 1, now standard time: the same local day, 25 hours long.
      totalsRow({ bucket_start: "2026-11-02T04:00:00Z", calls: 4 }),
      // Midnight on Nov 2.
      totalsRow({ bucket_start: "2026-11-02T05:00:00Z", calls: 8 }),
    ];

    const { starts, values } = binRows(
      "7d",
      now,
      rows,
      () => "all",
      (row) => row.calls,
    );
    const byDay = new Map(starts.map((start, index) => [start.getDate(), values[index].get("all") ?? 0]));

    expect(byDay.get(31)).toBe(1);
    expect(byDay.get(1)).toBe(6);
    expect(byDay.get(2)).toBe(8);
  });

  it("keeps a series per bar apart", () => {
    const rows = [
      totalsRow({ bucket_start: "2026-10-22T17:00:00Z", group: { model: "a" }, calls: 1 }),
      totalsRow({ bucket_start: "2026-10-22T17:00:00Z", group: { model: "b" }, calls: 3 }),
    ];

    const { starts, values } = binRows(
      "24h",
      NOW,
      rows,
      (row) => row.group.model ?? "",
      (row) => row.calls,
    );
    const index = starts.findIndex((start) => start.toISOString() === "2026-10-22T17:00:00.000Z");

    expect(Object.fromEntries(values[index])).toEqual({ a: 1, b: 3 });
  });
});
