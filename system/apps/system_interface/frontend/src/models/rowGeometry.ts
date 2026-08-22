/**
 * Measured geometry for a conversation's rendered rows, and the arithmetic that
 * turns it into reserved scroll space.
 *
 * This exists because the transcript reserves space for history it has not
 * loaded, and that space has to be sized in the unit the renderer works in. A
 * fixed constant per *event* is not that unit: the renderer groups a whole turn
 * into one row, so a tool-heavy turn of 50 events renders as a single
 * ProgressBlock. A page landing at a fraction of its reservation collapses the
 * scroll height in one frame, which is the transcript "jumping while reading
 * history".
 *
 * So nothing here guesses. A completed row's height, at a given viewport width,
 * is a fact -- measure it once, remember it, and reserve the real number. What is
 * left over (ranges this client has genuinely never rendered) is priced at a
 * rate the caller learned from what it *has* measured.
 *
 * Rows are held sorted by `start_offset` and never overlap, so every query is a
 * binary search plus a prefix-sum lookup. Kept DOM-free so the arithmetic is
 * unit-testable on its own.
 */

/**
 * One rendered row and the global transcript event range it covers.
 *
 * The range matters as much as the height: a query asks "how much scroll space
 * is above event N", and N usually falls *inside* a turn rather than on a row
 * boundary. Storing the range is what lets an offset resolve to the real row
 * that contains it instead of to a position that does not physically exist.
 */
export interface RowGeometry {
  row_key: string;
  /** Global index of the row's first event within the full transcript. */
  start_offset: number;
  /** Global index one past the row's last event (exclusive). */
  end_offset: number;
  height: number;
}

/** The JSON-able form persisted to IndexedDB and to the server. */
export interface GeometrySnapshot {
  rows: RowGeometry[];
}

/**
 * Height per event to start from, for a conversation with no measured rows at
 * all (a genuinely cold first paint). Every later rate is the caller's, learned
 * from what this transcript actually renders at.
 */
export const DEFAULT_EVENT_HEIGHT_PX = 160;

/** Sort key: rows are kept in transcript order and never overlap. */
function byStartOffset(a: RowGeometry, b: RowGeometry): number {
  return a.start_offset - b.start_offset;
}

/**
 * Index of the first row whose `start_offset` is >= `offset`, by binary search.
 * That is the insertion point, so `rows[result - 1]` is the last row starting
 * before `offset` (possibly straddling it).
 */
function lowerBound(rows: RowGeometry[], offset: number): number {
  let low = 0;
  let high = rows.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (rows[mid].start_offset < offset) {
      low = mid + 1;
    } else {
      high = mid;
    }
  }
  return low;
}

/**
 * A conversation's measured rows, with cumulative sums maintained alongside.
 *
 * Two parallel prefix arrays are kept so a query needs no scan: `#heightBefore[i]`
 * is the summed height of rows `[0, i)` and `#eventsBefore[i]` the summed event
 * count. Together they answer "measured pixels, and how many events those
 * pixels accounted for", which is what separates measured space from the gaps
 * that still need estimating.
 *
 * Coverage is deliberately allowed to be *sparse*. A client that read the head of
 * a conversation and then jumped to the tail has measured two islands with a
 * hole between them, and that is a normal state -- not an error to repair. Gaps
 * are filled with the learned estimate at query time.
 */
export class RowGeometryIndex {
  #rows: RowGeometry[] = [];
  // Cumulative sums over #rows, both of length #rows.length + 1 and rebuilt
  // lazily so a burst of recordRow calls costs one rebuild, not one per row.
  #heightBefore: number[] = [0];
  #eventsBefore: number[] = [0];
  #sumsAreStale = false;

  constructor(rows: RowGeometry[] = []) {
    this.#rows = [...rows].sort(byStartOffset);
    this.#sumsAreStale = true;
  }

  get rowCount(): number {
    return this.#rows.length;
  }

  /** The measured rows, in transcript order. Treat as read-only. */
  get rows(): readonly RowGeometry[] {
    return this.#rows;
  }

  #rebuildSums(): void {
    const heights: number[] = new Array(this.#rows.length + 1);
    const events: number[] = new Array(this.#rows.length + 1);
    heights[0] = 0;
    events[0] = 0;
    for (let i = 0; i < this.#rows.length; i++) {
      const row = this.#rows[i];
      heights[i + 1] = heights[i] + row.height;
      events[i + 1] = events[i] + Math.max(0, row.end_offset - row.start_offset);
    }
    this.#heightBefore = heights;
    this.#eventsBefore = events;
    this.#sumsAreStale = false;
  }

  #sums(): void {
    if (this.#sumsAreStale) {
      this.#rebuildSums();
    }
  }

  /**
   * Record one row's measured geometry, replacing every row it overlaps.
   *
   * Replacement is by *range* rather than by `row_key` or by `start_offset`
   * alone. The same rendered row is legitimately recorded under different
   * offsets as the loaded window moves -- the renderer clamps its first row's
   * start to the window start, so that row claims a different range once a
   * backfill or an eviction moves the window -- and matching on the start alone
   * would file the second description alongside the first. Two rows covering
   * the same events break the non-overlap invariant the prefix sums are built
   * on: the range's height would be counted twice and its events would be
   * missing from the unmeasured gap below it.
   *
   * Returns whether anything changed, so a caller can skip persisting a no-op.
   */
  recordRow(row: RowGeometry): boolean {
    const insertAt = lowerBound(this.#rows, row.start_offset);
    // Rows above this point start before the new one, and the invariant leaves
    // at most one of them able to reach into it.
    const previous = this.#rows[insertAt - 1];
    const first = previous !== undefined && previous.end_offset > row.start_offset ? insertAt - 1 : insertAt;
    let end = first;
    while (end < this.#rows.length && this.#rows[end].start_offset < row.end_offset) {
      end += 1;
    }
    const replaced = this.#rows[first];
    if (
      end - first === 1 &&
      replaced.row_key === row.row_key &&
      replaced.start_offset === row.start_offset &&
      replaced.end_offset === row.end_offset &&
      replaced.height === row.height
    ) {
      return false;
    }
    this.#rows.splice(first, end - first, row);
    this.#sumsAreStale = true;
    return true;
  }

  /**
   * Scroll space occupied by everything above the row containing `offset`.
   *
   * This is the number the transcript reserves for unloaded history, and the
   * whole point of the module. Measured rows contribute their real heights;
   * event ranges no row covers contribute `gap events * gapRate`. A row
   * straddling `offset` is excluded, so the result always lands on a row
   * boundary -- there is no position inside a rendered row to scroll to.
   */
  heightBefore(offset: number, gapRate: number): number {
    if (offset <= 0) {
      return 0;
    }
    this.#sums();
    const index = lowerBound(this.#rows, offset);
    // Exclude a straddling row: its content starts before `offset` but it
    // renders as one indivisible block, so the boundary above it is the answer.
    const previous = this.#rows[index - 1];
    const boundary = previous !== undefined && offset < previous.end_offset ? index - 1 : index;
    const measuredHeight = this.#heightBefore[boundary];
    const measuredEvents = this.#eventsBefore[boundary];
    const boundaryOffset = boundary < this.#rows.length ? this.#rows[boundary].start_offset : offset;
    // Events below `offset` that no measured row accounted for. Derived from the
    // boundary's own offset rather than from `offset` directly, so a straddling
    // row does not have its events counted as an unmeasured gap as well.
    const gapEvents = Math.max(0, Math.min(offset, boundaryOffset) - measuredEvents);
    // The rate is the caller's rather than derived here, because this index only
    // holds rows that have *settled* -- half a second behind first paint, and
    // folded in on whatever redraw happens next -- while the caller sees every
    // row that has been measured at all. A rate off settled rows alone leaves
    // the reserve at its cold value until the first settle lands and then
    // collapses it in one step, on whichever redraw comes next, which is
    // routinely the user's own scroll.
    return measuredHeight + gapEvents * gapRate;
  }

  /**
   * The inverse of `heightBefore`: the largest offset within `[0, maxOffset]`
   * whose reserved space is still at or below `height`.
   *
   * This is what maps a scrollbar position back to a place in the transcript, so
   * it must be the *exact* inverse of the function that sized the space -- if the
   * two disagree, a drag resolves to an offset far from where the thumb is, and
   * fires a jump the user never asked for. Rather than invert the arithmetic by
   * hand (which sparse coverage makes fiddly), this binary-searches
   * `heightBefore` itself, at the same `gapRate`, so the two cannot drift apart.
   */
  offsetAtHeight(height: number, maxOffset: number, gapRate: number): number {
    if (height <= 0 || maxOffset <= 0) {
      return 0;
    }
    let low = 0;
    let high = maxOffset;
    while (low < high) {
      const mid = Math.ceil((low + high) / 2);
      if (this.heightBefore(mid, gapRate) <= height) {
        low = mid;
      } else {
        high = mid - 1;
      }
    }
    return low;
  }

  /** The JSON-able form for persistence. */
  toSnapshot(): GeometrySnapshot {
    return { rows: [...this.#rows] };
  }
}

/**
 * Rebuild an index from a persisted snapshot, discarding anything malformed.
 *
 * Persisted data outlives the code that wrote it (IndexedDB entries survive
 * reloads, the server's copy survives deploys), so a shape change must degrade
 * to "measure it again" rather than throw during a paint. Rows that overlap a
 * previously accepted one are dropped for the same reason: the sums assume
 * non-overlapping ranges, and a corrupt entry must not be able to produce
 * nonsense geometry.
 */
export function geometryFromSnapshot(snapshot: unknown): RowGeometryIndex {
  if (snapshot === null || typeof snapshot !== "object" || !Array.isArray((snapshot as GeometrySnapshot).rows)) {
    return new RowGeometryIndex();
  }
  const accepted: RowGeometry[] = [];
  const valid: RowGeometry[] = [];
  for (const candidate of (snapshot as GeometrySnapshot).rows as unknown[]) {
    if (candidate === null || typeof candidate !== "object") {
      continue;
    }
    const row = candidate as Partial<RowGeometry>;
    if (
      typeof row.row_key !== "string" ||
      typeof row.start_offset !== "number" ||
      typeof row.end_offset !== "number" ||
      typeof row.height !== "number" ||
      !Number.isFinite(row.start_offset) ||
      !Number.isFinite(row.end_offset) ||
      !Number.isFinite(row.height) ||
      row.start_offset < 0 ||
      row.end_offset <= row.start_offset ||
      row.height <= 0
    ) {
      continue;
    }
    valid.push({
      row_key: row.row_key,
      start_offset: row.start_offset,
      end_offset: row.end_offset,
      height: row.height,
    });
  }
  valid.sort(byStartOffset);
  let lastEnd = 0;
  for (const row of valid) {
    if (row.start_offset < lastEnd) {
      continue;
    }
    accepted.push(row);
    lastEnd = row.end_offset;
  }
  return new RowGeometryIndex(accepted);
}
