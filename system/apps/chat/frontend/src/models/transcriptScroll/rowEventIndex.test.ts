import { describe, expect, it } from "vitest";
import { buildRowEventIndexes, rowIndexForEventIndex } from "./rowEventIndex";

describe("buildRowEventIndexes", () => {
  it("maps each row's anchor event to its global index", () => {
    const indexes = buildRowEventIndexes(["a", "c", "e"], ["a", "b", "c", "d", "e"], 100);
    expect(indexes).toEqual([100, 102, 104]);
  });

  it("inherits the previous row's index for a null anchor", () => {
    const indexes = buildRowEventIndexes(["a", null, "d"], ["a", "b", "c", "d"], 10);
    expect(indexes).toEqual([10, 10, 13]);
  });

  it("uses the window start for a leading unknown anchor", () => {
    const indexes = buildRowEventIndexes([null, "b"], ["a", "b"], 7);
    expect(indexes).toEqual([7, 8]);
  });

  it("inherits for an anchor id missing from the window", () => {
    const indexes = buildRowEventIndexes(["a", "gone", "b"], ["a", "b"], 0);
    expect(indexes).toEqual([0, 0, 1]);
  });
});

describe("rowIndexForEventIndex", () => {
  const rowStarts = [100, 102, 104, 104, 110];

  it("finds the last row starting at or before the event", () => {
    expect(rowIndexForEventIndex(rowStarts, 100)).toBe(0);
    expect(rowIndexForEventIndex(rowStarts, 101)).toBe(0);
    expect(rowIndexForEventIndex(rowStarts, 102)).toBe(1);
    expect(rowIndexForEventIndex(rowStarts, 104)).toBe(3);
    expect(rowIndexForEventIndex(rowStarts, 109)).toBe(3);
    expect(rowIndexForEventIndex(rowStarts, 500)).toBe(4);
  });

  it("clamps an index before the window to the first row", () => {
    expect(rowIndexForEventIndex(rowStarts, 5)).toBe(0);
  });

  it("returns -1 for an empty list", () => {
    expect(rowIndexForEventIndex([], 10)).toBe(-1);
  });
});
