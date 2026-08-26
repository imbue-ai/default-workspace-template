/**
 * Explicit viewport anchoring for expand/collapse toggles in the transcript.
 *
 * Native browser scroll anchoring picks its own anchor node, and when a toggled
 * block's row starts above the viewport the browser's choice is inconsistent --
 * sometimes the content under the reader's eyes jumps by the toggled height.
 * This helper makes the behavior deterministic: whatever line sat at the top of
 * the viewport before the toggle sits there after it.
 *
 * The anchor is the deepest element at the viewport's top-left (the "first
 * sentence on screen"), measured before the mutation and re-measured after; the
 * scroll container is adjusted by the difference. When the mutation removes the
 * anchor itself from the document (collapsing the very block being read), the
 * caller-provided fallback element -- the toggle header, which always survives
 * its own toggle -- anchors instead, so the header the user clicked stays put.
 *
 * The scrollTop write fires the container's onscroll like any programmatic
 * scroll; the transcript's follow-state machinery classifies it exactly as it
 * classifies native anchoring's own adjustments, so no special-casing is needed
 * (a reader mid-history is already not following the tail, and a reader at the
 * tail sees no adjustment because everything toggled is above the tail pin).
 */

/** The minimal element surface this module reads, for testability under jsdom
 *  (which has no layout): production passes real elements. */
export interface AnchorableElement {
  getBoundingClientRect(): { top: number };
  isConnected: boolean;
}

export interface AnchorScrollContainer {
  getBoundingClientRect(): { top: number; left: number; width: number };
  scrollTop: number;
}

/** How far inside the container's top-left corner the anchor probe lands. Deep
 *  enough to clear borders and sticky edges, small enough to stay on the first
 *  visible line. */
const ANCHOR_PROBE_INSET_PX = 12;

/**
 * Run `mutate` (which must synchronously change the DOM -- callers wrap
 * mithril toggles with `m.redraw.sync()`) while holding the first visible line
 * of `scrollEl` fixed. `fallback` anchors when the mutation removes the probed
 * anchor from the document.
 */
export function withViewportAnchor(
  scrollEl: AnchorScrollContainer | null,
  fallback: AnchorableElement | null,
  mutate: () => void,
): void {
  if (scrollEl === null) {
    mutate();
    return;
  }
  const probed = probeAnchor(scrollEl);
  const anchor = probed ?? fallback;
  if (anchor === null) {
    mutate();
    return;
  }
  const fallbackTopBefore = fallback?.getBoundingClientRect().top ?? 0;
  const topBefore = anchor.getBoundingClientRect().top;
  mutate();
  applyAnchorAdjustment(scrollEl, {
    anchor,
    topBefore,
    fallback,
    fallbackTopBefore,
  });
}

interface AnchorAdjustmentInput {
  anchor: AnchorableElement;
  topBefore: number;
  fallback: AnchorableElement | null;
  fallbackTopBefore: number;
}

/** Adjust `scrollEl.scrollTop` so the surviving anchor's top is unchanged.
 *  Exported for tests; production goes through `withViewportAnchor`. */
export function applyAnchorAdjustment(scrollEl: AnchorScrollContainer, input: AnchorAdjustmentInput): void {
  let delta: number;
  if (input.anchor.isConnected) {
    delta = input.anchor.getBoundingClientRect().top - input.topBefore;
  } else if (input.fallback !== null && input.fallback.isConnected) {
    delta = input.fallback.getBoundingClientRect().top - input.fallbackTopBefore;
  } else {
    return;
  }
  if (delta !== 0) {
    scrollEl.scrollTop += delta;
  }
}

/** The deepest document element at the viewport's top-left probe point, or null
 *  when it falls outside this container (an overlay, another panel). */
function probeAnchor(scrollEl: AnchorScrollContainer): AnchorableElement | null {
  // Real containers are HTMLElements; the interface split exists for tests
  // (which may run without a DOM at all, hence the typeof guard).
  if (typeof HTMLElement === "undefined" || !(scrollEl instanceof HTMLElement)) {
    return null;
  }
  const rect = scrollEl.getBoundingClientRect();
  const probeX = rect.left + Math.min(ANCHOR_PROBE_INSET_PX + 68, rect.width / 2);
  const probed = document.elementFromPoint(probeX, rect.top + ANCHOR_PROBE_INSET_PX);
  if (probed === null || !scrollEl.contains(probed) || probed === scrollEl) {
    return null;
  }
  return probed;
}

/** The transcript scroll container enclosing `element`, for toggle call sites
 *  (both the chat panel and the subagent view render into `.app-content`). */
export function findScrollContainer(element: HTMLElement): HTMLElement | null {
  return element.closest(".app-content");
}
