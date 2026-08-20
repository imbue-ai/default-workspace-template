/**
 * Measured geometry for a conversation's rendered rows, and the arithmetic that
 * turns it into reserved scroll space.
 *
 * This exists because the transcript reserves space for history it has not
 * loaded, and the unit it reserved in was wrong. The old code sized that space
 * at a fixed constant per *event*, but the renderer groups a whole turn into one
 * row: a tool-heavy turn of 50 events renders as a single ProgressBlock. A page
 * landing at a fraction of its reservation collapsed the scroll height in one
 * frame, which is the transcript "jumping while reading history".
 *
 * The fix is to stop guessing. A completed row's height, at a given viewport
 * width, is a fact -- so measure it once, remember it, and reserve the real
 * number. What is left over (ranges this client has genuinely never rendered) is
 * estimated from what it *has* measured, not from a hardcoded constant.
 *
 * Rows are held sorted by `start_offset` and never overlap, so every query is a
 * binary search plus a prefix-sum lookup. Kept DOM-free so the arithmetic -- the
 * part that was subtly wrong for months -- is unit-testable on its own.
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
 * Fallback height per event, used only when a conversation has no measured rows
 * at all (a genuinely cold first paint). Every later estimate comes from
 * `learnedEventHeight`, which reflects what this transcript actually renders at.
 * The old code used this number for *everything*, which is the bug this module
 * exists to remove.
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

/** The median of a non-empty list, averaging the middle pair when even. */
function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
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
   * Record (or replace) one row's measured geometry.
   *
   * Replacement is by `start_offset` rather than by `row_key`, because the same
   * key can be re-measured at a different height and because the offset is what
   * the sums are indexed on. Returns whether anything changed, so a caller can
   * skip persisting a no-op.
   */
  recordRow(row: RowGeometry): boolean {
    const index = lowerBound(this.#rows, row.start_offset);
    const existing = this.#rows[index];
    if (existing !== undefined && existing.start_offset === row.start_offset) {
      if (
        existing.height === row.height &&
        existing.end_offset === row.end_offset &&
        existing.row_key === row.row_key
      ) {
        return false;
      }
      this.#rows[index] = row;
      this.#sumsAreStale = true;
      return true;
    }
    this.#rows.splice(index, 0, row);
    this.#sumsAreStale = true;
    return true;
  }

  /**
   * Drop every row starting at or after `offset`.
   *
   * Called when a row's height changes retroactively -- a subagent card
   * upgrading once its late linkage lands, or a harness re-serialising an event
   * in place. Everything below such a row shifts, so the measured heights below
   * it are no longer trustworthy; everything above is untouched and stays. The
   * dropped range reverts to the learned estimate until it is rendered again.
   */
  invalidateFrom(offset: number): number {
    const index = lowerBound(this.#rows, offset);
    if (index >= this.#rows.length) {
      return 0;
    }
    const removed = this.#rows.length - index;
    this.#rows.length = index;
    this.#sumsAreStale = true;
    return removed;
  }

  /**
   * Pixels per event, learned from the rows actually measured.
   *
   * The median rather than the mean: one enormous row (a long pasted file, a
   * wide tool output) would drag a mean far above what typical history renders
   * at, and the estimate is used to size ranges that are mostly ordinary turns.
   * Falls back to the cold default only when nothing has been measured yet.
   */
  learnedEventHeight(): number {
    const perEvent: number[] = [];
    for (const row of this.#rows) {
      const events = row.end_offset - row.start_offset;
      if (events > 0 && row.height > 0) {
        perEvent.push(row.height / events);
      }
    }
    return perEvent.length === 0 ? DEFAULT_EVENT_HEIGHT_PX : median(perEvent);
  }

  /**
   * The row containing `offset`, or null when no measured row covers it.
   *
   * Used to land a jump on a real row rather than partway inside one, and to
   * decide whether a query lands in measured space or in a gap.
   */
  rowAtOffset(offset: number): RowGeometry | null {
    const index = lowerBound(this.#rows, offset);
    // lowerBound lands on the first row starting at or after `offset`; an exact
    // hit is that row, otherwise the only candidate is the one before it.
    const exact = this.#rows[index];
    if (exact !== undefined && exact.start_offset === offset) {
      return exact;
    }
    const previous = this.#rows[index - 1];
    if (previous !== undefined && offset < previous.end_offset) {
      return previous;
    }
    return null;
  }

  /**
   * Scroll space occupied by everything above the row containing `offset`.
   *
   * This is the number the transcript reserves for unloaded history, and the
   * whole point of the module. Measured rows contribute their real heights;
   * event ranges no row covers contribute `gap events * learnedEventHeight`.
   * A row straddling `offset` is excluded, so the result always lands on a row
   * boundary -- there is no position inside a rendered row to scroll to.
   */
  heightBefore(offset: number, gapRate?: number): number {
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
    // The caller may supply the rate to price gaps at. It has a better one: this
    // index only holds rows that have *settled*, which lags first paint by half
    // a second, while the caller can see every row that has been measured at all.
    // Pricing gaps off settled rows alone means the reserve stays at its cold
    // default until the first settle lands and then collapses in one step -- and
    // that step falls on whichever redraw happens next, which is routinely the
    // user's own scroll.
    return measuredHeight + gapEvents * (gapRate ?? this.learnedEventHeight());
  }

  /**
   * The inverse of `heightBefore`: the largest offset whose reserved space is
   * still at or above `height`, searched within `[0, maxOffset]`.
   *
   * This is what maps a scrollbar position back to a place in the transcript, so
   * it must be the *exact* inverse of the function that sized the space -- if the
   * two disagree, a drag can resolve to an offset far from where the thumb is,
   * which is how the old code fired jumps the user never asked for. Rather than
   * invert the arithmetic by hand (which sparse coverage makes fiddly), this
   * binary-searches `heightBefore` itself, so the two cannot drift apart.
   */
  offsetAtHeight(height: number, maxOffset: number, gapRate?: number): number {
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

  /** Total measured height of every recorded row. */
  totalMeasuredHeight(): number {
    this.#sums();
    return this.#heightBefore[this.#rows.length];
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
  const candidates = [...((snapshot as GeometrySnapshot).rows as unknown[])];
  const valid: RowGeometry[] = [];
  for (const candidate of candidates) {
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
