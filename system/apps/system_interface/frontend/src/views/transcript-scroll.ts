/**
 * Shared scroll controller for the virtualized transcript views (ChatPanel and
 * SubagentView), which otherwise duplicated this machinery.
 *
 * It owns the scroll-follow state (scroll position, whether the user has scrolled
 * up off the tail, the drag flag) and encapsulates:
 *  - tail following: while at the bottom, pin to the tail on each redraw (deferred
 *    while a drag/selection is in progress, and yielding to an in-flight wheel-up);
 *  - scroll-event handling: update the follow state, distinguishing a real
 *    scroll-up from a browser shrink-clamp (see scrollFollow);
 *  - the pointer-drag and viewport-resize lifecycle.
 *
 * While scrolled up the default is native scroll anchoring (the views' spacers opt
 * out of being chosen as its anchor). The controller writes scrollTop on its own
 * account only for the tail pin; every other write goes through `pinTo`, so the two
 * views' deliberate repositioning is visible in one place rather than spread across
 * the file.
 *
 * The two views differ only in a few spots, injected via `config`: dockview
 * visibility gating, whether newer history exists below the loaded window, and any
 * extra work to run after a user scroll (ChatPanel's paging). Everything else --
 * including the phantom regions, paging, eviction and jump logic -- stays in
 * ChatPanel; this controller is deliberately unaware of it. Windowing and row
 * measurement belong to the virtualizer (see transcriptVirtualizer), so the one
 * decision that repeatedly went wrong here -- whether to follow the tail -- has a
 * single owner.
 */

import m from "mithril";
import { nextUserScrolledUp } from "../models/scrollFollow";

// Within this many pixels of the bottom counts as "at the tail".
const SCROLL_BOTTOM_THRESHOLD_PX = 40;

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
  /** Current scrollTop (in the scroll container's own coordinates). */
  readonly scrollTop: number;
  /** Cached viewport height, refreshed by the resize observer registered when the
   *  container is attached. Zero until the container has reported a height, which
   *  is what a hidden dockview tab reports; callers read the live `clientHeight`
   *  in that case. */
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
  /** Apply the tail-follow pin if following (no-op while scrolled up -- native
   *  anchoring handles that). Call from oncreate/onupdate. */
  applyScrollPosition(element: HTMLElement): void;
  /** Pin scrollTop to an exact position, syncing the follow bookkeeping so the write
   *  does not read back as a user scroll. ChatPanel uses it twice: to land an offset
   *  jump at the top of the freshly loaded rows, and to hold the reader on the row
   *  they were reading once the window has been recomputed. */
  pinTo(element: HTMLElement, top: number): void;
  /** Reset scroll + follow state (e.g. switching to a different agent). */
  reset(): void;
}

export function createTranscriptScroll(config: TranscriptScrollConfig = {}): TranscriptScroll {
  const isVisible = config.isVisible ?? (() => true);
  const getHasMoreAfter = config.getHasMoreAfter ?? (() => false);
  const onUserScroll = config.onUserScroll ?? (() => {});

  let scrollEl: HTMLElement | null = null;
  let scrollTop = 0;
  let previousScrollTop = 0;
  let viewportHeight = 0;
  let userScrolledUp = false;
  // Last observed scrollHeight, to tell a browser shrink-clamp from a real scroll-up.
  let lastScrollHeight = 0;
  // A pointer button is held over the transcript (a drag, likely a selection): the
  // tail pin defers so streaming output doesn't scroll content out from under it.
  let isPointerDown = false;
  let viewportResizeObserver: ResizeObserver | null = null;
  let pointerReleaseListener: (() => void) | null = null;
  // Scroll events this controller caused by writing scrollTop itself, which are
  // still queued. See writeScrollTop for why they must not reach the follow rule.
  let pendingSelfInflictedScrolls = 0;

  /**
   * Write scrollTop, and remember that the event it fires is ours.
   *
   * Every write here schedules a scroll event indistinguishable, at the DOM
   * level, from the user moving the wheel. Keeping `previousScrollTop` in step
   * is not enough to make it harmless: that only makes the event look like *no
   * movement*, and "no movement while near the bottom" is exactly the condition
   * the follow rule re-attaches on. So a correction applied a little way above
   * the tail re-armed following and the next redraw slammed the view back down
   * -- which is what made scrolling up a short distance impossible rather than
   * merely inaccurate. Counting the write lets onScroll drop that event instead
   * of deriving anything from it.
   */
  function writeScrollTop(element: HTMLElement, top: number): void {
    if (element.scrollTop === top) {
      // No change, so no event is coming; counting one would swallow the user's
      // next real scroll.
      return;
    }
    pendingSelfInflictedScrolls += 1;
    element.scrollTop = top;
    scrollTop = element.scrollTop;
    previousScrollTop = element.scrollTop;
    lastScrollHeight = element.scrollHeight;
    // A write that is clamped, coalesced, or dropped may never deliver its
    // event. Clearing on the next frame keeps one lost event from silently
    // swallowing a real scroll for the rest of the session.
    requestAnimationFrame(() => {
      pendingSelfInflictedScrolls = 0;
    });
  }

  function applyTailFollow(element: HTMLElement): void {
    if (isPointerDown) {
      return;
    }
    // Honor an unprocessed user wheel-up whose scroll event hasn't fired yet: if the
    // live scrollTop is above where we last pinned, the user is scrolling up, so stop
    // pinning. `min(scrollTop, maxScroll)` distinguishes this from the browser
    // clamping scrollTop after the content shrank, which is still-at-bottom.
    const maxScroll = element.scrollHeight - element.clientHeight;
    if (element.scrollTop < Math.min(scrollTop, maxScroll) - 1) {
      userScrolledUp = true;
      scrollTop = element.scrollTop;
      previousScrollTop = element.scrollTop;
      return;
    }
    writeScrollTop(element, element.scrollHeight);
  }

  return {
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
      if (pendingSelfInflictedScrolls > 0) {
        // Our own write coming back. Record where it left us, but derive nothing
        // from it: it carries no user intent, and treating it as one is what let
        // a correction re-attach the view to the tail underneath the reader.
        pendingSelfInflictedScrolls -= 1;
        previousScrollTop = element.scrollTop;
        scrollTop = element.scrollTop;
        lastScrollHeight = element.scrollHeight;
        return;
      }
      // applyScrollPosition keeps previousScrollTop in lockstep with its own
      // programmatic pins, so only a genuine user scroll registers as movement.
      const didScrollUp = element.scrollTop < previousScrollTop;
      const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < SCROLL_BOTTOM_THRESHOLD_PX;
      // A shrink-clamp looks like a scroll-up but carries no user intent; the follow
      // state must be preserved rather than re-derived (see scrollFollow).
      const isClamp = didScrollUp && element.scrollHeight < lastScrollHeight && atBottom;
      previousScrollTop = element.scrollTop;
      scrollTop = element.scrollTop;
      userScrolledUp = nextUserScrolledUp({
        didScrollUp,
        isNearBottom: atBottom,
        hasMoreAfter: getHasMoreAfter(),
        isClamp,
        wasUserScrolledUp: userScrolledUp,
      });
      lastScrollHeight = element.scrollHeight;
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
      // Same test as the observer below, for the same reason: a height of zero
      // is a tab that is not on screen, not a viewport worth caching.
      if (element.clientHeight > 0) {
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
      // Judged on the measurement rather than on visibility, for the reason
      // transcriptVirtualizer's rect filter spells out: a panel mounted as an
      // inactive tab gets exactly one notification -- the resize when the tab is
      // shown -- and that lands after layout, while the visibility attr only
      // reaches the component on the next redraw. Rejecting it on visibility
      // would throw away the panel's only chance at a viewport size, and nothing
      // re-delivers it. A zero height IS what a hidden tab reports, so rejecting
      // zero keeps the cache from being poisoned while hidden, which is what the
      // visibility test was really for.
      viewportResizeObserver = new ResizeObserver(() => {
        if (scrollEl === null) {
          return;
        }
        const height = scrollEl.clientHeight;
        if (height <= 0 || height === viewportHeight) {
          return;
        }
        viewportHeight = height;
        m.redraw();
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
      scrollEl = null;
    },

    applyScrollPosition(element: HTMLElement): void {
      // While the panel is hidden (an inactive dockview tab) the element is
      // zero-sized; acting on that would clobber the retained scrollTop to 0. The
      // browser preserves scrollTop across hide/show, so skipping keeps it intact.
      if (!isVisible()) {
        return;
      }
      // While scrolled up the app writes nothing -- native scroll anchoring holds the
      // viewport. Only the tail pin writes scrollTop.
      if (!userScrolledUp) {
        applyTailFollow(element);
      }
      lastScrollHeight = element.scrollHeight;
    },

    pinTo(element: HTMLElement, top: number): void {
      writeScrollTop(element, top);
    },

    reset(): void {
      scrollTop = 0;
      previousScrollTop = 0;
      userScrolledUp = false;
      lastScrollHeight = 0;
      isPointerDown = false;
    },
  };
}
