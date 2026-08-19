/**
 * Shared scroll controller for the virtualized transcript views (ChatPanel and
 * SubagentView), which otherwise duplicated this machinery.
 *
 * It owns the scroll-follow state (scroll position, whether the user has scrolled
 * up off the tail, the drag flag) and the row measurer, and encapsulates:
 *  - tail following: while at the bottom, pin to the tail on each redraw (deferred
 *    while a drag/selection is in progress, and yielding to an in-flight wheel-up);
 *  - scroll-event handling: update the follow state from user-evidenced
 *    movement only (see scrollFollow);
 *  - the pointer-drag and viewport-resize lifecycle.
 *
 * Viewport stability while scrolled up is view-specific and lives outside this
 * controller: ChatPanel derives scrollTop from a reading anchor and applies the
 * correction through `pinTo` (native anchoring cannot survive its window remaps),
 * while SubagentView -- whose whole transcript is loaded, so geometry only drifts
 * by small measurements -- still relies on native scroll anchoring (its spacers
 * opt out). The controller itself writes scrollTop only through the deliberate
 * pins: following the tail, and `pinTo`.
 *
 * The two views differ only in a few spots, injected via `config`: dockview
 * visibility gating, whether newer history exists below the loaded window, and any
 * extra work to run after a user scroll (ChatPanel's paging). Everything else --
 * including the phantom regions, paging, eviction and jump logic -- stays in
 * ChatPanel; this controller is deliberately unaware of it.
 */

import m from "mithril";
import { createRowMeasurer, type RowMeasurer } from "./row-measurement";
import { nextUserScrolledUp } from "../models/scrollFollow";

// Touching the bottom, allowing for fractional scrollTop against integer
// scrollHeight/clientHeight. A tolerance, not a proximity band: following is
// only ever engaged while actually at the tail.
const SCROLL_BOTTOM_TOUCH_PX = 2;
// A scroll event is attributed to the user only while a matching-direction
// input (wheel) was seen within this window, or a pointer drag is in flight.
// Covers the gap between an input event and its coalesced scroll event, and
// macOS momentum keeps emitting wheel events, so a fling stays attributed.
const USER_INPUT_ATTRIBUTION_MS = 250;
// Ignore scroll deltas at or below this as sub-pixel layout wobble.
const SCROLL_DELTA_EPSILON_PX = 1;

export interface TranscriptScrollConfig {
  /** Whether the scroll element is really visible and sized (dockview collapses an
   *  inactive tab to zero, and acting on that would corrupt the retained position).
   *  Default: always visible. */
  isVisible?: () => boolean;
  /** Whether newer history exists below the loaded window (only true for ChatPanel
   *  after an offset jump moved the window off the live tail). Default: false. */
  getHasMoreAfter?: () => boolean;
  /** Extra work to run at the end of a user scroll (ChatPanel: drive paging).
   *  Default: nothing. */
  onUserScroll?: (element: HTMLElement) => void;
}

export interface TranscriptScroll {
  readonly rowMeasurer: RowMeasurer;
  /** Current scrollTop (in the scroll container's own coordinates). */
  readonly scrollTop: number;
  /** Cached viewport height, refreshed on measure/resize. */
  readonly viewportHeight: number;
  /** True when the user has scrolled up off the live tail (do not follow). */
  userScrolledUp: boolean;
  /** The scroll container element, or null before mount. */
  readonly scrollEl: HTMLElement | null;

  /** onscroll handler for the scroll container. */
  onScroll(event: Event): void;
  /** onpointerdown handler for the scroll container (marks a drag in progress). */
  onPointerDown(): void;
  /** Register listeners + observers against the scroll element (idempotent); call
   *  from the container's oncreate and onupdate. */
  attach(element: HTMLElement): void;
  /** Tear down listeners + observers; call from onremove. */
  detach(): void;
  /** Apply the tail-follow pin if following (no-op while scrolled up -- the view's
   *  own anchoring owns the position then). Call from oncreate/onupdate. */
  applyScrollPosition(element: HTMLElement): void;
  /** Pin scrollTop to an exact position once (ChatPanel: land an offset jump at the
   *  top of the freshly loaded rows), syncing the follow bookkeeping. */
  pinTo(element: HTMLElement, top: number): void;
  /** Refresh the cached viewport height and schedule a measure pass. */
  scheduleMeasure(): void;
  /** Reset scroll + follow state (e.g. switching to a different agent). */
  reset(): void;
}

export function createTranscriptScroll(config: TranscriptScrollConfig = {}): TranscriptScroll {
  const isVisible = config.isVisible ?? (() => true);
  const getHasMoreAfter = config.getHasMoreAfter ?? (() => false);
  const onUserScroll = config.onUserScroll ?? (() => {});

  const rowMeasurer = createRowMeasurer();
  let scrollEl: HTMLElement | null = null;
  let scrollTop = 0;
  let previousScrollTop = 0;
  let viewportHeight = 0;
  let userScrolledUp = false;
  // A pointer button is held over the transcript (a drag, likely a selection): the
  // tail pin defers so streaming output doesn't scroll content out from under it.
  let isPointerDown = false;
  // When the user last expressed scroll intent in each direction (wheel events;
  // a held pointer counts as both, covering scrollbar drags). Scroll events
  // without matching recent input are machinery -- pin echoes, clamps, native
  // anchoring, layout wobble -- and never change the follow state.
  let lastUpInputAt = Number.NEGATIVE_INFINITY;
  let lastDownInputAt = Number.NEGATIVE_INFINITY;
  let viewportResizeObserver: ResizeObserver | null = null;
  let wheelListener: ((event: WheelEvent) => void) | null = null;
  let pointerReleaseListener: (() => void) | null = null;

  function hasRecentUpInput(): boolean {
    return isPointerDown || performance.now() - lastUpInputAt < USER_INPUT_ATTRIBUTION_MS;
  }

  function hasRecentDownInput(): boolean {
    return isPointerDown || performance.now() - lastDownInputAt < USER_INPUT_ATTRIBUTION_MS;
  }

  function applyTailFollow(element: HTMLElement): void {
    if (isPointerDown) {
      return;
    }
    // Honor an unprocessed user wheel-up whose scroll event hasn't fired yet: if
    // the live scrollTop is above where we last pinned AND the user recently
    // wheeled up, they are scrolling up, so stop pinning. The input check is what
    // separates a real wheel-up from machinery that also lowers scrollTop with no
    // user involved (a shrink-clamp, or the clamp-then-regrow wobble of rows
    // re-measuring at the tail) -- without it, streaming at the tail detaches
    // itself within seconds. `min(scrollTop, maxScroll)` additionally excludes
    // the plain clamp-to-new-maximum case.
    const maxScroll = element.scrollHeight - element.clientHeight;
    if (hasRecentUpInput() && element.scrollTop < Math.min(scrollTop, maxScroll) - 1) {
      userScrolledUp = true;
      scrollTop = element.scrollTop;
      previousScrollTop = element.scrollTop;
      return;
    }
    element.scrollTop = element.scrollHeight;
    scrollTop = element.scrollTop;
    previousScrollTop = element.scrollTop;
  }

  return {
    get rowMeasurer() {
      return rowMeasurer;
    },
    get scrollTop() {
      return scrollTop;
    },
    get viewportHeight() {
      return viewportHeight;
    },
    get userScrolledUp() {
      return userScrolledUp;
    },
    set userScrolledUp(value: boolean) {
      userScrolledUp = value;
    },
    get scrollEl() {
      return scrollEl;
    },

    onScroll(event: Event): void {
      const element = event.target as HTMLElement;
      // applyScrollPosition keeps previousScrollTop in lockstep with its own
      // programmatic pins, so only a genuine scroll registers as movement -- and
      // movement counts as the USER's only with matching recent input, so pin
      // echoes, clamps, native anchoring adjustments and layout wobble can never
      // flip the follow state (see scrollFollow).
      const delta = element.scrollTop - previousScrollTop;
      const isAtBottom = element.scrollHeight - element.scrollTop - element.clientHeight <= SCROLL_BOTTOM_TOUCH_PX;
      previousScrollTop = element.scrollTop;
      scrollTop = element.scrollTop;
      userScrolledUp = nextUserScrolledUp({
        userMovedUp: delta < -SCROLL_DELTA_EPSILON_PX && hasRecentUpInput(),
        userMovedDown: delta > SCROLL_DELTA_EPSILON_PX && hasRecentDownInput(),
        isAtBottom,
        hasMoreAfter: getHasMoreAfter(),
        wasUserScrolledUp: userScrolledUp,
      });
      onUserScroll(element);
    },

    onPointerDown(): void {
      isPointerDown = true;
    },

    attach(element: HTMLElement): void {
      scrollEl = element;
      if (pointerReleaseListener !== null) {
        return; // already registered
      }
      if (isVisible()) {
        viewportHeight = element.clientHeight;
      }
      // Clear the drag flag on release. Listen on window, not the panel, because the
      // pointer is often released outside the transcript; redraw so the deferred tail
      // pin re-applies immediately.
      pointerReleaseListener = () => {
        if (isPointerDown) {
          isPointerDown = false;
          m.redraw();
        }
      };
      window.addEventListener("pointerup", pointerReleaseListener);
      window.addEventListener("pointercancel", pointerReleaseListener);
      wheelListener = (event: WheelEvent) => {
        if (event.deltaY < 0) {
          lastUpInputAt = performance.now();
        } else if (event.deltaY > 0) {
          lastDownInputAt = performance.now();
        }
      };
      element.addEventListener("wheel", wheelListener, { passive: true });
      viewportResizeObserver = new ResizeObserver(() => {
        if (scrollEl === null || !isVisible()) {
          return;
        }
        if (scrollEl.clientHeight !== viewportHeight) {
          viewportHeight = scrollEl.clientHeight;
          m.redraw();
        }
      });
      viewportResizeObserver.observe(element);
    },

    detach(): void {
      if (viewportResizeObserver !== null) {
        viewportResizeObserver.disconnect();
        viewportResizeObserver = null;
      }
      if (pointerReleaseListener !== null) {
        window.removeEventListener("pointerup", pointerReleaseListener);
        window.removeEventListener("pointercancel", pointerReleaseListener);
        pointerReleaseListener = null;
      }
      if (wheelListener !== null && scrollEl !== null) {
        scrollEl.removeEventListener("wheel", wheelListener);
        wheelListener = null;
      }
      scrollEl = null;
    },

    applyScrollPosition(element: HTMLElement): void {
      // While the panel is hidden (an inactive dockview tab) the element is
      // zero-sized; acting on that would clobber the retained scrollTop to 0. The
      // browser preserves scrollTop across hide/show, so skipping keeps it intact.
      if (!isVisible()) {
        return;
      }
      // While scrolled up the controller writes nothing -- the view's own anchoring
      // (ChatPanel's derived pin, SubagentView's native anchoring) holds the
      // viewport. Only the tail pin writes scrollTop here.
      if (!userScrolledUp) {
        applyTailFollow(element);
      }
    },

    pinTo(element: HTMLElement, top: number): void {
      element.scrollTop = top;
      scrollTop = element.scrollTop;
      previousScrollTop = element.scrollTop;
    },

    scheduleMeasure(): void {
      if (scrollEl !== null && isVisible()) {
        viewportHeight = scrollEl.clientHeight;
      }
      rowMeasurer.scheduleMeasure(() => scrollEl);
    },

    reset(): void {
      scrollTop = 0;
      previousScrollTop = 0;
      userScrolledUp = false;
      isPointerDown = false;
      rowMeasurer.reset();
    },
  };
}
