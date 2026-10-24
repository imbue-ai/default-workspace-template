import { describe, expect, it } from "vitest";

import { formatCalls, formatDollars, formatTurns } from "./format";

describe("formatCalls", () => {
  it("says call for one and calls otherwise", () => {
    expect(formatCalls(1)).toBe("1 call");
    expect(formatCalls(0)).toBe("0 calls");
    expect(formatCalls(12_300)).toBe("12.3K calls");
  });
});

describe("formatTurns", () => {
  it("says turn for one and turns otherwise", () => {
    expect(formatTurns(1)).toBe("1 turn");
    expect(formatTurns(12)).toBe("12 turns");
  });
});

describe("formatDollars", () => {
  it("keeps two significant digits below a cent", () => {
    expect(formatDollars(0.0042)).toBe("$0.0042");
    expect(formatDollars(15.618)).toBe("$15.62");
    expect(formatDollars(0)).toBe("$0");
  });
});
