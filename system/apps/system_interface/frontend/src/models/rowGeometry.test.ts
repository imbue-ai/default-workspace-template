import { describe, expect, it } from "vitest";
import { DEFAULT_EVENT_HEIGHT_PX, RowGeometryIndex, geometryFromSnapshot, type RowGeometry } from "./rowGeometry";

function row(start: number, end: number, height: number, key = `row-${start}`): RowGeometry {
  return { row_key: key, start_offset: start, end_offset: end, height };
}

// The rate unmeasured events are priced at. Always the caller's, so that the
// reservation and the scrollbar mapping back through it cannot be priced
// differently; these pass a memorable one to keep the arithmetic readable.
const RATE = 10;

describe("RowGeometryIndex", () => {
  it("prices the whole transcript at the caller's rate before anything is measured", () => {
    const index = new RowGeometryIndex();
    expect(index.rowCount).toBe(0);
    expect(index.heightBefore(0, DEFAULT_EVENT_HEIGHT_PX)).toBe(0);
    expect(index.heightBefore(500, DEFAULT_EVENT_HEIGHT_PX)).toBe(500 * DEFAULT_EVENT_HEIGHT_PX);
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
    expect(index.heightBefore(10, RATE)).toBe(250);
  });

  it("replaces a row re-recorded at a different range instead of keeping both", () => {
    // The renderer clamps its first row's start to the loaded window's start, so
    // the same row claims [100, 110) while the window begins at 100 and [103, 110)
    // once a backfill puts older rows above it. Keeping both would count that
    // row's height twice and hide its events from the gap below.
    const index = new RowGeometryIndex();
    index.recordRow(row(100, 110, 340, "turn-a"));
    expect(index.recordRow(row(103, 110, 340, "turn-a"))).toBe(true);
    expect(index.rowCount).toBe(1);
    expect(index.rows[0].start_offset).toBe(103);
    // 340px for the row itself; the 103 events above it were never measured.
    expect(index.heightBefore(110, RATE)).toBe(340 + 103 * RATE);
  });

  it("replaces every row a re-measured range swallows", () => {
    // Turn grouping can merge what used to render as several rows into one (a
    // skill expansion, a late subagent link). The merged row owns the range now.
    const index = new RowGeometryIndex([row(0, 10, 100), row(10, 20, 100), row(20, 30, 100)]);
    expect(index.recordRow(row(10, 30, 400, "merged"))).toBe(true);
    expect(index.rows.map((r) => r.row_key)).toEqual(["row-0", "merged"]);
    expect(index.heightBefore(30, RATE)).toBe(500);
  });

  it("sums measured heights for a fully measured prefix", () => {
    const index = new RowGeometryIndex([row(0, 10, 340), row(10, 20, 340)]);
    expect(index.heightBefore(10, RATE)).toBe(340);
    expect(index.heightBefore(20, RATE)).toBe(680);
  });

  it("resolves an offset inside a row to the boundary above it", () => {
    // The core reason row ranges are stored: event 5 sits inside a turn that
    // renders as one indivisible block, so there is no scroll position "5 events
    // into" it -- the answer is the space above the whole row.
    const index = new RowGeometryIndex([row(0, 10, 340), row(10, 20, 340)]);
    expect(index.heightBefore(5, RATE)).toBe(0);
    expect(index.heightBefore(15, RATE)).toBe(340);
  });

  it("prices ranges no row covers at the rate, and measured ones at their height", () => {
    // The 20 events above offset 30 that no row covers are priced at the rate;
    // the 10 below it contribute the 100px they actually rendered at.
    const index = new RowGeometryIndex([row(0, 10, 100)]);
    expect(index.heightBefore(30, RATE)).toBe(100 + 20 * RATE);
  });

  it("handles sparse coverage with a hole between measured islands", () => {
    // Reading the head and then jumping to the tail leaves a genuine hole. That
    // is a normal state, not corruption: measured islands keep their real
    // heights and only the hole is priced at the rate.
    const index = new RowGeometryIndex([row(0, 10, 100), row(100, 110, 100)]);
    // 20 measured events (200px) + 130 gap events at the rate.
    expect(index.heightBefore(150, RATE)).toBe(200 + 130 * RATE);
  });

  it("sizes a tool-heavy turn from its real height, not per event", () => {
    // The regression this module exists to prevent. Fifty events collapse into
    // one ProgressBlock row that renders at 340px, where pricing the same range
    // per event reserves 50 * 160 = 8000px -- a ~7.7k px collapse the moment the
    // page lands.
    const index = new RowGeometryIndex([row(0, 50, 340)]);
    expect(index.heightBefore(50, DEFAULT_EVENT_HEIGHT_PX)).toBe(340);
  });

  it("never reports negative height for a non-positive offset", () => {
    const index = new RowGeometryIndex([row(0, 10, 100)]);
    expect(index.heightBefore(0, RATE)).toBe(0);
    expect(index.heightBefore(-50, RATE)).toBe(0);
  });

  it("maps a reserved height back through the rate that sized it", () => {
    // offsetAtHeight is the inverse of heightBefore, so the two have to price
    // the gap identically; at a different rate the same scrollbar position
    // resolves somewhere else entirely, which is the jump nobody asked for.
    const index = new RowGeometryIndex([row(0, 10, 100), row(100, 110, 100)]);
    const reserved = index.heightBefore(100, RATE);

    expect(index.offsetAtHeight(reserved, 200, RATE)).toBeGreaterThanOrEqual(100);
    expect(index.offsetAtHeight(reserved, 200, RATE * 4)).toBeLessThan(100);
  });
});

describe("geometryFromSnapshot", () => {
  it("round-trips a snapshot", () => {
    const original = new RowGeometryIndex([row(0, 10, 100), row(10, 25, 340)]);
    const restored = geometryFromSnapshot(original.toSnapshot());
    expect(restored.rows).toEqual(original.rows);
    expect(restored.heightBefore(25, RATE)).toBe(440);
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
