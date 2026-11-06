import { describe, expect, it } from "vitest";

import { storeAppRecord } from "./testing/records";
import { VERDICT_LABELS, buildText, diskText, formatMb, memoryText } from "./room";

describe("formatMb", () => {
  it("writes KB under a megabyte, MB below a gigabyte, and GB from there", () => {
    expect([0.5, 21, 950.25, 1024, 1536].map(formatMb)).toEqual(["512 KB", "21 MB", "950.3 MB", "1.0 GB", "1.5 GB"]);
  });

  it("never writes a size as zero KB", () => {
    expect(formatMb(0.0001)).toBe("1 KB");
  });
});

describe("VERDICT_LABELS", () => {
  it("names every verdict the store sends", () => {
    expect(VERDICT_LABELS).toEqual({ FITS: "Fits", TIGHT: "Tight", WONT_FIT: "Won't fit" });
  });
});

describe("diskText and memoryText", () => {
  it("prefer what was measured, then the estimate, then say what is unknown", () => {
    expect(diskText(storeAppRecord("a"))).toBe("100 MB");
    expect(diskText(storeAppRecord("a", { is_added: true }))).toBe("about 100 MB");
    expect(diskText(storeAppRecord("a", { disk_mb: null }))).toBe("unknown");
    expect(diskText(storeAppRecord("a", { measured_disk_mb: 21 }))).toBe("21 MB measured");
    expect(memoryText(storeAppRecord("a"))).toBe("50 MB in use");
    expect(memoryText(storeAppRecord("a", { memory_in_use_mb: null }))).toBe("unknown until it runs");
    expect(memoryText(storeAppRecord("a", { memory_in_use_mb: null, peak_memory_mb: 312 }))).toBe("312 MB measured");
  });
});

describe("buildText", () => {
  it("says what building needs, and nothing for an app that is not built", () => {
    expect(buildText(storeAppRecord("a"))).toBe("");
    expect(buildText(storeAppRecord("a", { build_memory_mb: 3000, build_disk_mb: 1100 }))).toBe(
      "Installing builds it from its source first, which needs about 2.9 GB of memory and 1.1 GB of disk for a minute or so. The fit counts that too.",
    );
  });
});
