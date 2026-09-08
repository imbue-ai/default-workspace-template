/**
 * Mapping between rendered rows and global event indexes. Rows are derived from
 * the loaded event window (a turn can collapse many events into one row), while
 * the fill planner, scrollbar virtual regions, and persistence all speak global
 * event indexes -- this is the bridge.
 */

import type { EventIndex } from "./types";

/**
 * The global event index each row starts at. Rows whose anchor event is unknown
 * (a section with no opening user message) inherit the previous row's index;
 * a leading unknown gets the window start. Events missing from the window map
 * (already superseded ids) inherit likewise, so the result is always monotonic
 * non-decreasing and aligned with `firstOffset`.
 */
export function buildRowEventIndexes(
  rowAnchorEventIds: readonly (string | null)[],
  windowEventIds: readonly string[],
  firstOffset: number,
): EventIndex[] {
  const windowIndexById = new Map<string, number>();
  for (let i = 0; i < windowEventIds.length; i++) {
    windowIndexById.set(windowEventIds[i], i);
  }
  const rowEventIndexes: EventIndex[] = new Array(rowAnchorEventIds.length);
  let previousIndex = firstOffset;
  for (let i = 0; i < rowAnchorEventIds.length; i++) {
    const anchorEventId = rowAnchorEventIds[i];
    const windowIndex = anchorEventId === null ? undefined : windowIndexById.get(anchorEventId);
    const eventIndex = windowIndex === undefined ? previousIndex : firstOffset + windowIndex;
    rowEventIndexes[i] = eventIndex;
    previousIndex = eventIndex;
  }
  return rowEventIndexes;
}

/**
 * The row containing a global event index: the last row starting at or before
 * it. -1 for an empty list; clamped to the first row for an index before the
 * window.
 */
export function rowIndexForEventIndex(rowEventIndexes: readonly EventIndex[], eventIndex: EventIndex): number {
  if (rowEventIndexes.length === 0) {
    return -1;
  }
  let low = 0;
  let high = rowEventIndexes.length - 1;
  // Binary search for the last row with rowEventIndexes[row] <= eventIndex.
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    if (rowEventIndexes[mid] <= eventIndex) {
      low = mid;
    } else {
      high = mid - 1;
    }
  }
  return low;
}
