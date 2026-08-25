/**
 * Physical-layer geometry: exact row positions from the height table, anchor
 * resolution, and the visible-row window.
 *
 * The invariant the whole engine leans on:
 *   scrollTopForAnchor(anchorFromViewport(viewport)) === viewport.scrollTopPx
 * exactly, for any geometry -- so re-deriving scrollTop from the anchor each
 * redraw holds the anchored row pixel-stable no matter what changed above it.
 */

import type { PhysicalGeometry, RowKey, ScrollAnchor, Viewport } from "./types";

export interface GeometryRowInput {
  readonly key: RowKey;
  /** Exact measurement (live or offscreen), or null while unmeasured. */
  readonly measuredPx: number | null;
  /** Fallback height used until the row is measured. */
  readonly estimatePx: number;
}

export interface VisibleRowRange {
  readonly startIndex: number; // inclusive
  readonly endIndex: number; // exclusive
}

// Key -> row index, memoized per geometry instance so lookups are O(1) without
// widening the plain PhysicalGeometry data type.
const rowIndexByKeyByGeometry = new WeakMap<PhysicalGeometry, Map<RowKey, number>>();

export function buildPhysicalGeometry(rows: readonly GeometryRowInput[]): PhysicalGeometry {
  const rowKeys: RowKey[] = new Array(rows.length);
  const rowTops: number[] = new Array(rows.length);
  let runningTop = 0;
  let unmeasuredCount = 0;
  for (let i = 0; i < rows.length; i++) {
    rowKeys[i] = rows[i].key;
    rowTops[i] = runningTop;
    if (rows[i].measuredPx === null) {
      unmeasuredCount += 1;
    }
    runningTop += rows[i].measuredPx ?? rows[i].estimatePx;
  }
  return { rowKeys, rowTops, totalHeightPx: runningTop, unmeasuredCount };
}

export function rowIndexOfKey(geometry: PhysicalGeometry, key: RowKey): number | null {
  let index = rowIndexByKeyByGeometry.get(geometry);
  if (index === undefined) {
    index = new Map(geometry.rowKeys.map((rowKey, i) => [rowKey, i]));
    rowIndexByKeyByGeometry.set(geometry, index);
  }
  return index.get(key) ?? null;
}

export function rowHeightAt(geometry: PhysicalGeometry, index: number): number {
  const bottom = index + 1 < geometry.rowTops.length ? geometry.rowTops[index + 1] : geometry.totalHeightPx;
  return bottom - geometry.rowTops[index];
}

// First index in `rowTops` whose value is >= target (== rowTops.length when none).
function lowerBoundRowTop(rowTops: readonly number[], target: number): number {
  let low = 0;
  let high = rowTops.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (rowTops[mid] >= target) {
      high = mid;
    } else {
      low = mid + 1;
    }
  }
  return low;
}

/**
 * The anchor for a viewport: the first row whose top edge is at/below the
 * viewport top, with its offset from the viewport top. When the viewport sits
 * below every row top (inside a tall trailing row), the last row anchors with a
 * negative offset so anchoring never fails while content exists. Null only for
 * empty geometry.
 */
export function anchorFromViewport(geometry: PhysicalGeometry, viewport: Viewport): ScrollAnchor | null {
  if (geometry.rowKeys.length === 0) {
    return null;
  }
  const viewportTopContentPx = viewport.scrollTopPx - viewport.spacerTopPx;
  const firstAtOrBelow = lowerBoundRowTop(geometry.rowTops, viewportTopContentPx);
  const anchorIndex = firstAtOrBelow < geometry.rowKeys.length ? firstAtOrBelow : geometry.rowKeys.length - 1;
  return {
    rowKey: geometry.rowKeys[anchorIndex],
    offsetPx: geometry.rowTops[anchorIndex] - viewportTopContentPx,
  };
}

/** The scrollTop that puts `anchor` back at its stored offset; null if the row is gone. */
export function scrollTopForAnchor(
  geometry: PhysicalGeometry,
  anchor: ScrollAnchor,
  spacerTopPx: number,
): number | null {
  const anchorIndex = rowIndexOfKey(geometry, anchor.rowKey);
  if (anchorIndex === null) {
    return null;
  }
  return spacerTopPx + geometry.rowTops[anchorIndex] - anchor.offsetPx;
}

/**
 * The contiguous row range intersecting the viewport plus overscan. When the
 * viewport is entirely below all content (a transient overshoot while heights
 * settle), fill backward from the last row until the viewport plus overscan is
 * covered, so the rendered height stays stable instead of collapsing to one row.
 */
export function computeVisibleRowRange(
  geometry: PhysicalGeometry,
  viewport: Viewport,
  overscanPx: number,
): VisibleRowRange {
  const rowCount = geometry.rowKeys.length;
  if (rowCount === 0) {
    return { startIndex: 0, endIndex: 0 };
  }
  const windowTopContentPx = viewport.scrollTopPx - viewport.spacerTopPx - overscanPx;
  const windowBottomContentPx = viewport.scrollTopPx - viewport.spacerTopPx + viewport.heightPx + overscanPx;

  // First row whose bottom edge crosses into the window. Row i's bottom is
  // rowTops[i + 1] (or the total height for the last row), so searching rowTops
  // shifted by one gives the same boundary.
  let startIndex = lowerBoundRowTop(geometry.rowTops, windowTopContentPx);
  if (
    startIndex > 0 &&
    geometry.rowTops[startIndex - 1] + rowHeightAt(geometry, startIndex - 1) > windowTopContentPx
  ) {
    startIndex -= 1;
  }

  if (startIndex >= rowCount) {
    // Entirely below all content: backward fill.
    const coveragePx = viewport.heightPx + 2 * overscanPx;
    let filledPx = 0;
    let backwardStart = rowCount - 1;
    for (let i = rowCount - 1; i >= 0; i--) {
      backwardStart = i;
      filledPx += rowHeightAt(geometry, i);
      if (filledPx >= coveragePx) {
        break;
      }
    }
    return { startIndex: backwardStart, endIndex: rowCount };
  }

  // One past the last row whose top edge is above the window bottom.
  let endIndex = lowerBoundRowTop(geometry.rowTops, windowBottomContentPx);
  if (endIndex <= startIndex) {
    // The viewport sits within a single tall row.
    endIndex = startIndex + 1;
  }
  return { startIndex, endIndex: Math.min(endIndex, rowCount) };
}
