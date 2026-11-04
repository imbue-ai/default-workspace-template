import { describe, expect, it } from "vitest";

import { formatItemCount, formatModified, formatSize } from "./format";

describe("format", () => {
  it("writes sizes in decimal units", () => {
    expect(formatSize(1)).toBe("1 byte");
    expect(formatSize(999)).toBe("999 bytes");
    expect(formatSize(1500)).toBe("1.5 KB");
    expect(formatSize(25_000_000)).toBe("25 MB");
  });

  it("counts items", () => {
    expect(formatItemCount(1)).toBe("1 item");
    expect(formatItemCount(3)).toBe("3 items");
  });

  it("says today and yesterday, and the date otherwise", () => {
    const now = new Date(2026, 9, 5, 15, 0);
    expect(formatModified(new Date(2026, 9, 5, 9, 5).getTime(), now, "en-US")).toBe("Today at 9:05 AM");
    expect(formatModified(new Date(2026, 9, 4, 23, 59).getTime(), now, "en-US")).toBe("Yesterday at 11:59 PM");
    expect(formatModified(new Date(2026, 0, 2, 8, 0).getTime(), now, "en-US")).toBe("Jan 2, 2026 at 8:00 AM");
  });
});
