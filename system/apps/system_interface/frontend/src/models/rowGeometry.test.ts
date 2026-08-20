import { describe, expect, it } from "vitest";
import {
  DEFAULT_EVENT_HEIGHT_PX,
  RowGeometryIndex,
  geometryFromSnapshot,
  type RowGeometry,
} from "./rowGeometry";

function row(start: number, end: number, height: number, key = `row-${start}`): RowGeometry {
  return { row_key: key, start_offset: start, end_offset: end, height };
}

describe("RowGeometryIndex", () => {
  it("reports no height and the cold default before anything is measured", () => {
    const index = new RowGeometryIndex();
    expect(index.rowCount).toBe(0);
    expect(index.heightBefore(0)).toBe(0);
    expect(index.heightBefore(500)).toBe(500 * DEFAULT_EVENT_HEIGHT_PX);
    expect(index.learnedEventHeight()).toBe(DEFAULT_EVENT_HEIGHT_PX);
  });

  it("keeps rows sorted regardless of the order they are recorded in", () => {
    const index = new RowGeometryIndex();
    index.recordRow(row(20, 30, 100));
    index.recordRow(row(0, 10, 100));
    index.recordRow(row(10, 20, 100));
    expect(index.rows.map((r) => r.start_offset)).toEqual([0, 10, 20]);
  });

  it("replaces a row measured again at a new height, and reports a no-op as unchanged", () => {
    const index = new RowGeometryIndex();
    expect(index.recordRow(row(0, 10, 100))).toBe(true);
    // Same row, same numbers: nothing to persist.
    expect(index.recordRow(row(0, 10, 100))).toBe(false);
    // Re-measured taller: replaced in place, not appended.
    expect(index.recordRow(row(0, 10, 250))).toBe(true);
    expect(index.rowCount).toBe(1);
    expect(index.totalMeasuredHeight()).toBe(250);
  });

  it("sums measured heights for a fully measured prefix", () => {
    const index = new RowGeometryIndex([row(0, 10, 340), row(10, 20, 340)]);
    expect(index.heightBefore(10)).toBe(340);
    expect(index.heightBefore(20)).toBe(680);
    expect(index.totalMeasuredHeight()).toBe(680);
  });

  it("resolves an offset inside a row to the boundary above it", () => {
    // The core reason row ranges are stored: event 5 sits inside a turn that
    // renders as one indivisible block, so there is no scroll position "5 events
    // into" it -- the answer is the space above the whole row.
    const index = new RowGeometryIndex([row(0, 10, 340), row(10, 20, 340)]);
    expect(index.heightBefore(5)).toBe(0);
    expect(index.heightBefore(15)).toBe(340);
  });

  it("estimates unmeasured ranges from the learned per-event height", () => {
    // 10 events measured at 100px total -> 10px/event learned. The 20 events
    // above offset 30 that no row covers are estimated at that rate, NOT at the
    // cold default.
    const index = new RowGeometryIndex([row(0, 10, 100)]);
    expect(index.learnedEventHeight()).toBe(10);
    expect(index.heightBefore(30)).toBe(100 + 20 * 10);
  });

  it("handles sparse coverage with a hole between measured islands", () => {
    // Reading the head and then jumping to the tail leaves a genuine hole. That
    // is a normal state, not corruption: measured islands keep their real
    // heights and only the hole is estimated.
    const index = new RowGeometryIndex([row(0, 10, 100), row(100, 110, 100)]);
    expect(index.learnedEventHeight()).toBe(10);
    // 20 measured events (200px) + 130 gap events at 10px.
    expect(index.heightBefore(150)).toBe(200 + 130 * 10);
  });

  it("sizes a tool-heavy turn from its real height, not per event", () => {
    // The regression this module exists to prevent. Fifty events collapse into
    // one ProgressBlock row that renders at 340px. The old per-event constant
    // would have reserved 50 * 160 = 8000px for the same range -- a ~7.7k px
    // collapse the moment the page landed.
    const index = new RowGeometryIndex([row(0, 50, 340)]);
    expect(index.heightBefore(50)).toBe(340);
    expect(index.heightBefore(50)).not.toBe(50 * DEFAULT_EVENT_HEIGHT_PX);
  });

  it("learns a median per-event height that one huge row cannot drag upward", () => {
    // Three ordinary rows at 10px/event and one enormous outlier at 500px/event.
    // A mean would land near 132px/event; the median stays with the typical row.
    const index = new RowGeometryIndex([
      row(0, 10, 100),
      row(10, 20, 100),
      row(20, 30, 100),
      row(30, 40, 5000),
    ]);
    expect(index.learnedEventHeight()).toBe(10);
  });

  it("finds the row containing an offset and reports a gap as uncovered", () => {
    const index = new RowGeometryIndex([row(0, 10, 100), row(100, 110, 100)]);
    expect(index.rowAtOffset(0)?.row_key).toBe("row-0");
    expect(index.rowAtOffset(9)?.row_key).toBe("row-0");
    expect(index.rowAtOffset(100)?.row_key).toBe("row-100");
    expect(index.rowAtOffset(10)).toBeNull();
    expect(index.rowAtOffset(50)).toBeNull();
  });

  it("drops rows at and after the invalidation point, keeping those above", () => {
    // A subagent card upgrading, or a harness re-serialising an event, changes
    // that row's height and shifts everything below it. Above it is untouched.
    const index = new RowGeometryIndex([row(0, 10, 100), row(10, 20, 100), row(20, 30, 100)]);
    expect(index.invalidateFrom(10)).toBe(2);
    expect(index.rowCount).toBe(1);
    expect(index.totalMeasuredHeight()).toBe(100);
  });

  it("treats invalidation past the last row as a no-op", () => {
    const index = new RowGeometryIndex([row(0, 10, 100)]);
    expect(index.invalidateFrom(999)).toBe(0);
    expect(index.rowCount).toBe(1);
  });

  it("falls back to estimating a range that was invalidated", () => {
    const index = new RowGeometryIndex([row(0, 10, 100), row(10, 20, 100)]);
    expect(index.heightBefore(20)).toBe(200);
    index.invalidateFrom(10);
    // The dropped range reverts to the learned rate (10px/event) rather than
    // vanishing from the reserved space entirely.
    expect(index.heightBefore(20)).toBe(100 + 10 * 10);
  });

  it("never reports negative height for a non-positive offset", () => {
    const index = new RowGeometryIndex([row(0, 10, 100)]);
    expect(index.heightBefore(0)).toBe(0);
    expect(index.heightBefore(-50)).toBe(0);
  });
});

describe("geometryFromSnapshot", () => {
  it("round-trips a snapshot", () => {
    const original = new RowGeometryIndex([row(0, 10, 100), row(10, 25, 340)]);
    const restored = geometryFromSnapshot(original.toSnapshot());
    expect(restored.rows).toEqual(original.rows);
    expect(restored.heightBefore(25)).toBe(440);
  });

  it("returns an empty index for anything that is not a snapshot", () => {
    // Persisted data outlives the code that wrote it, so a shape change must
    // degrade to "measure it again" rather than throw during a paint.
    expect(geometryFromSnapshot(null).rowCount).toBe(0);
    expect(geometryFromSnapshot(undefined).rowCount).toBe(0);
    expect(geometryFromSnapshot("nonsense").rowCount).toBe(0);
    expect(geometryFromSnapshot({}).rowCount).toBe(0);
    expect(geometryFromSnapshot({ rows: "no" }).rowCount).toBe(0);
  });

  it("discards malformed rows but keeps the valid ones", () => {
    const restored = geometryFromSnapshot({
      rows: [
        row(0, 10, 100),
        { row_key: "bad-height", start_offset: 10, end_offset: 20, height: 0 },
        { row_key: "bad-range", start_offset: 30, end_offset: 30, height: 50 },
        { row_key: "negative", start_offset: -1, end_offset: 5, height: 50 },
        { row_key: "missing-height", start_offset: 40, end_offset: 50 },
        null,
        row(50, 60, 200),
      ],
    });
    expect(restored.rows.map((r) => r.row_key)).toEqual(["row-0", "row-50"]);
  });

  it("discards overlapping rows so the sums cannot go nonsense", () => {
    const restored = geometryFromSnapshot({
      rows: [row(0, 20, 100), row(10, 30, 100, "overlaps"), row(30, 40, 100)],
    });
    expect(restored.rows.map((r) => r.row_key)).toEqual(["row-0", "row-30"]);
  });
});
