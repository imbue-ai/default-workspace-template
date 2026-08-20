/**
 * Mithril binding for `@tanstack/virtual-core`, shared by the chat panel and the
 * subagent view.
 *
 * The library is framework-agnostic and ships the DOM observers it needs
 * (`observeElementRect`, `observeElementOffset`, `elementScroll`), so this is
 * thin: supply the scroll element, redraw when the virtualizer changes, and
 * re-push the options that move (row count, reserved space, the selection pin)
 * on every render.
 *
 * Three deliberate departures from the library's defaults:
 *
 * 1. **Measurement is ours.** The stock `measureElement` rounds the border-box
 *    size to an integer and falls back to `offsetHeight`; both are
 *    device-pixel-snapped and so depend on a row's fractional vertical position,
 *    which is what produced a continuous ~1px jitter here before. Rows are
 *    measured by `rowMeasurement` instead and reported by index, so the library
 *    never reads the DOM for sizes and the message renderers need no index
 *    attribute -- the existing `id` contract is untouched.
 *
 * 2. **Spacer rendering, not absolute positioning.** Items stay in normal flow
 *    with a spacer above and below, because the transcript relies on the
 *    browser's own scroll anchoring to hold the viewport while the user reads,
 *    and absolutely-positioned content does not participate in it.
 *
 * 3. **Overscan in pixels.** The library counts items; a transcript row ranges
 *    from a one-line chip to a whole progress block, so a fixed count would mean
 *    wildly different coverage depending on what is on screen.
 */

import m from "mithril";
import {
  Virtualizer,
  defaultRangeExtractor,
  elementScroll,
  observeElementOffset,
  observeElementRect,
} from "@tanstack/virtual-core";
import type { Range, Rect, VirtualItem } from "@tanstack/virtual-core";

/**
 * Pixels rendered above and below the viewport so scrolling does not flash blank
 * before the next redraw fills the window.
 */
export const OVERSCAN_PX = 800;

/**
 * Overscan used before any row has been measured, chosen so the first paint
 * covers roughly OVERSCAN_PX at a typical assistant row height.
 */
const DEFAULT_OVERSCAN_ROWS = 4;

export interface TranscriptVirtualizerConfig {
  /** The scroll container, or null before mount. */
  getScrollElement: () => HTMLElement | null;
  /** Number of rows currently in the list. */
  getCount: () => number;
  /** Stable key for a row, so a prepended page shifts indices without
   *  invalidating measurements (this is what makes backfill safe). */
  getRowKey: (index: number) => string;
  /** Height for a row: its measurement if it has one, else an estimate. */
  estimateSize: (index: number) => number;
  /** Reserved space above the loaded rows for history not yet fetched. */
  getPaddingStart: () => number;
  /** Reserved space below the loaded rows. */
  getPaddingEnd: () => number;
  /** Row indices that must stay mounted regardless of the viewport (the rows a
   *  live text selection touches). */
  getPinnedIndices: () => number[];
  /** Whether the view is really visible and sized. A hidden dockview tab
   *  collapses to zero, and windowing against that would corrupt the retained
   *  scroll position. */
  isEnabled: () => boolean;
}

export interface TranscriptVirtualizer {
  /** Push the options that change each render, then recompute. Call from the
   *  view before reading items. */
  sync(): void;
  /** The rows to render, each carrying its offset and size. */
  getVirtualItems(): VirtualItem[];
  /** Total scroll height, including the reserved regions. */
  getTotalSize(): number;
  /** Spacer height standing in for everything above the first rendered row. */
  getLeadingSpace(): number;
  /** Spacer height standing in for everything below the last rendered row. */
  getTrailingSpace(): number;
  /** Report a row's newly measured height. */
  resizeRow(index: number, height: number): void;
  /** Scroll so the given row index is at the top of the viewport. */
  scrollToIndex(index: number): void;
  /** Scroll to an exact offset. */
  scrollToOffset(offset: number): void;
  /** Whether a scroll gesture is currently in flight. */
  isScrolling(): boolean;
  /** Current scroll offset as the virtualizer sees it. */
  scrollOffset(): number;
  /** Register with the DOM; call once the scroll element exists. */
  mount(): void;
  /** Tear down observers. */
  unmount(): void;
  /** Forget every measurement (switching to a different agent). */
  reset(): void;
}

export function createTranscriptVirtualizer(config: TranscriptVirtualizerConfig): TranscriptVirtualizer {
  let cleanup: (() => void) | null = null;
  // Assigned immediately after construction. The options builder reads the
  // instance's own measurements to size the overscan, but the instance is built
  // from those options -- so the reference is threaded rather than closed over,
  // and the one read guards for the moment it is still null.
  let virtualizerRef: Virtualizer<HTMLElement, Element> | null = null;

  /** Convert the pixel overscan budget into an item count using the average
   *  measured row height, so the rendered margin stays roughly OVERSCAN_PX
   *  regardless of row mix. */
  function overscanRows(): number {
    const sizes = [...(virtualizerRef?.itemSizeCache.values() ?? [])];
    if (sizes.length === 0) {
      return DEFAULT_OVERSCAN_ROWS;
    }
    let total = 0;
    for (const size of sizes) {
      total += size;
    }
    const average = total / sizes.length;
    if (!Number.isFinite(average) || average <= 0) {
      return DEFAULT_OVERSCAN_ROWS;
    }
    return Math.max(2, Math.ceil(OVERSCAN_PX / average));
  }

  /**
   * Keep the selection's rows in the rendered set even when the viewport has
   * moved far away, so scrolling or streaming past a selection does not unmount
   * its endpoints and collapse it. Only those rows are added -- not the
   * arbitrarily many between them and the viewport -- so a selection survives at
   * any distance for a bounded cost.
   */
  function extractRange(range: Range): number[] {
    const visible = defaultRangeExtractor(range);
    const pinned = config.getPinnedIndices();
    if (pinned.length === 0) {
      return visible;
    }
    const combined = new Set(visible);
    for (const index of pinned) {
      if (index >= 0 && index < range.count) {
        combined.add(index);
      }
    }
    return [...combined].sort((a, b) => a - b);
  }

  function buildOptions() {
    return {
      count: config.getCount(),
      getScrollElement: config.getScrollElement,
      estimateSize: config.estimateSize,
      getItemKey: (index: number) => config.getRowKey(index),
      overscan: overscanRows(),
      paddingStart: config.getPaddingStart(),
      paddingEnd: config.getPaddingEnd(),
      rangeExtractor: extractRange,
      scrollToFn: elementScroll,
      // While the view is hidden (an inactive dockview tab) the scroll element is
      // collapsed to zero and reports zero for its size and offset. Feeding that
      // through would recompute the window against a zero-height viewport and
      // unmount every row, so the tab would lose the place the user had scrolled
      // to. Both observers are filtered instead, freezing the last good geometry
      // for the duration -- the browser preserves the real scrollTop across
      // hide/show, so the frozen window is still correct when it comes back.
      //
      // Note this is NOT the library's `enabled` option: that empties the range
      // outright, which is the same lost-place failure by a different route.
      observeElementRect: (instance: Virtualizer<HTMLElement, Element>, cb: (rect: Rect) => void) =>
        observeElementRect(instance, (rect) => {
          if (!config.isEnabled() || rect.height <= 0) {
            return;
          }
          cb(rect);
        }),
      observeElementOffset: (
        instance: Virtualizer<HTMLElement, Element>,
        cb: (offset: number, isScrolling: boolean) => void,
      ) =>
        observeElementOffset(instance, (offset, isScrolling) => {
          if (!config.isEnabled()) {
            return;
          }
          cb(offset, isScrolling);
        }),
      onChange: (_instance: Virtualizer<HTMLElement, Element>, sync: boolean) => {
        // A synchronous change is one the library made while already inside an
        // event it is handling; redrawing then would re-enter mithril's render
        // from within that handler, so defer those to the next frame.
        if (sync) {
          requestAnimationFrame(() => m.redraw());
        } else {
          m.redraw();
        }
      },
    };
  }

  const virtualizer = new Virtualizer<HTMLElement, Element>(buildOptions());
  virtualizerRef = virtualizer;

  /**
   * Never compensate scroll here. The library adjusts scrollTop when an item
   * above the viewport changes size, which is a reasonable default but only half
   * the problem: the reserved space for unloaded history moves too, and the two
   * routinely cancel. The view holds the reader's position by anchoring on the
   * row they are reading, which covers both, so leaving this on would mean two
   * mechanisms writing scrollTop for overlapping reasons and double-correcting.
   */
  virtualizer.shouldAdjustScrollPositionOnItemSizeChange = () => false;

  return {
    sync(): void {
      virtualizer.setOptions(buildOptions());
      virtualizer._willUpdate();
    },

    getVirtualItems: () => virtualizer.getVirtualItems(),

    getTotalSize: () => virtualizer.getTotalSize(),

    getLeadingSpace(): number {
      const items = virtualizer.getVirtualItems();
      // `start` already includes paddingStart, so the leading spacer is exactly
      // the first rendered row's offset.
      return items.length === 0 ? config.getPaddingStart() : items[0].start;
    },

    getTrailingSpace(): number {
      const items = virtualizer.getVirtualItems();
      if (items.length === 0) {
        return config.getPaddingEnd();
      }
      return Math.max(0, virtualizer.getTotalSize() - items[items.length - 1].end);
    },

    resizeRow(index: number, height: number): void {
      virtualizer.resizeItem(index, height);
    },

    scrollToIndex(index: number): void {
      virtualizer.scrollToIndex(index, { align: "start" });
    },

    scrollToOffset(offset: number): void {
      virtualizer.scrollToOffset(offset, { align: "start" });
    },

    isScrolling: () => virtualizer.isScrolling,

    scrollOffset: () => virtualizer.scrollOffset ?? 0,

    mount(): void {
      if (cleanup !== null) {
        return;
      }
      cleanup = virtualizer._didMount();
    },

    unmount(): void {
      cleanup?.();
      cleanup = null;
    },

    reset(): void {
      virtualizer.itemSizeCache.clear();
      virtualizer.measurementsCache = [];
      virtualizer.measure();
    },
  };
}
