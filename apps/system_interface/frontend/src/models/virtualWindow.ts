/**
 * Pure windowing math for the virtualized message list.
 *
 * Given the heights of an ordered list of rows and the current scroll position,
 * computes which contiguous slice of rows intersects the viewport (plus an
 * overscan margin) and how much vertical padding stands in for the rows above
 * and below that slice. Keeping this free of the DOM makes the non-trivial part
 * of virtualization unit-testable; the component only has to feed it measured
 * heights and render the result.
 */

export interface VirtualWindowInput {
  /** Number of rows in the list. */
  count: number;
  /** Height in pixels of row `index` (measured if known, else an estimate). */
  getHeight: (index: number) => number;
  /** Current scrollTop of the scroll container. */
  scrollTop: number;
  /** Visible height of the scroll container. */
  viewportHeight: number;
  /** Extra pixels rendered above and below the viewport to avoid blank flashes. */
  overscanPx: number;
  /**
   * Inclusive row-index range that must stay rendered even when it lies outside
   * the viewport window -- rows holding a live text selection, so scrolling or
   * streaming past them does not unmount their DOM and collapse the selection.
   * Clamped to `[0, count)` internally, so a caller may pass a stale range
   * (e.g. from a selection made before the last data change) without guarding.
   */
  pinnedRange?: { start: number; end: number } | null;
}

export interface VirtualWindowResult {
  /** First row to render (inclusive). */
  startIndex: number;
  /** One past the last row to render (exclusive). */
  endIndex: number;
  /** Spacer height standing in for rows [0, startIndex). */
  topPad: number;
  /** Spacer height standing in for rows [endIndex, count). */
  bottomPad: number;
  /** Total height of all rows (topPad + rendered + bottomPad). */
  totalHeight: number;
}

/**
 * Compute the visible row window and the surrounding spacer heights.
 *
 * The window is the maximal contiguous run of rows whose vertical extent
 * overlaps `[scrollTop - overscanPx, scrollTop + viewportHeight + overscanPx]`.
 * When no row overlaps (e.g. an empty list) the window is empty and both pads
 * collapse so the spacers still sum to the true total height.
 */
export function computeVisibleWindow(input: VirtualWindowInput): VirtualWindowResult {
  const { count, getHeight, scrollTop, viewportHeight, overscanPx, pinnedRange } = input;

  if (count <= 0) {
    return { startIndex: 0, endIndex: 0, topPad: 0, bottomPad: 0, totalHeight: 0 };
  }

  const windowTop = scrollTop - overscanPx;
  const windowBottom = scrollTop + viewportHeight + overscanPx;

  let startIndex = -1;
  let endIndex = 0;
  let offset = 0;

  for (let i = 0; i < count; i++) {
    const height = getHeight(i);
    const rowTop = offset;
    const rowBottom = offset + height;

    // First row whose bottom edge crosses into the (over-scanned) viewport.
    if (startIndex === -1 && rowBottom > windowTop) {
      startIndex = i;
    }
    // Track the last row whose top edge is still above the viewport bottom.
    if (rowTop < windowBottom) {
      endIndex = i + 1;
    }
    offset += height;
  }

  const totalHeight = offset;

  if (startIndex === -1) {
    // The viewport is entirely below all content (scrolled past the end, e.g. a
    // transient scrollTop overshoot while measured heights settle). Fill backward
    // from the last row until the viewport plus overscan is covered, instead of
    // rendering only the final row: a one-row window collapses scrollHeight for a
    // frame, the browser clamps scrollTop, and everything remounts next frame -- a
    // visible bounce (and it drops any selection). A full backward slice keeps the
    // rendered height stable across the overshoot.
    const coverage = viewportHeight + 2 * overscanPx;
    let filled = 0;
    startIndex = count - 1;
    for (let i = count - 1; i >= 0; i--) {
      startIndex = i;
      filled += getHeight(i);
      if (filled >= coverage) {
        break;
      }
    }
    endIndex = count;
  } else if (endIndex <= startIndex) {
    // endIndex can lag startIndex when the viewport sits within a single tall row.
    endIndex = startIndex + 1;
  }

  // Expand the window to keep pinned rows mounted. Clamp defensively so a stale
  // range (rows removed since it was captured) can never produce an out-of-bounds
  // slice; the caller is free to pass yesterday's indices.
  if (pinnedRange) {
    const pinStart = Math.max(0, Math.min(pinnedRange.start, count - 1));
    const pinEnd = Math.max(0, Math.min(pinnedRange.end, count - 1));
    startIndex = Math.min(startIndex, pinStart);
    endIndex = Math.max(endIndex, pinEnd + 1);
  }

  // Pads are the exact height sums of the rows the window excludes on each side,
  // so topPad + rendered + bottomPad always reconstructs the total height.
  let topPad = 0;
  for (let i = 0; i < startIndex; i++) {
    topPad += getHeight(i);
  }
  let bottomPad = 0;
  for (let i = endIndex; i < count; i++) {
    bottomPad += getHeight(i);
  }

  return { startIndex, endIndex, topPad, bottomPad, totalHeight };
}
