/**
 * Pure decision for whether a virtualized transcript should follow the live tail
 * (auto-scroll to the bottom on each redraw) or stay put.
 *
 * It keys off USER-EVIDENCED movement only: the caller classifies a scroll event
 * as user movement when a matching-direction input (wheel, scrollbar drag) was
 * seen recently, so machinery-generated events -- programmatic pin echoes,
 * browser shrink-clamps, native anchoring adjustments, sub-pixel layout wobble
 * -- can never change the state. Any genuine upward movement detaches
 * immediately; following resumes only when a genuine downward movement lands
 * touching the bottom (within a couple px for fractional scrollTop -- a
 * position band is deliberately NOT used: position alone is not intent, and a
 * band both re-arms readers who merely pass near the tail and holds hostage
 * ones who stop just inside it). DOM-free so it is unit-testable.
 */

export interface FollowStateInput {
  // The scroll event moved the viewport up/down AND the caller attributes the
  // movement to the user (recent matching-direction input, or a held pointer
  // drag). Both false means machinery: preserve the state.
  userMovedUp: boolean;
  userMovedDown: boolean;
  // Touching the bottom (within the caller's fractional-pixel tolerance).
  isAtBottom: boolean;
  // Newer history exists on the server but isn't loaded (only after a jump moved
  // the window off the live tail), so the bottom of the window isn't the tail.
  hasMoreAfter: boolean;
  // The current follow state (true == not following).
  wasUserScrolledUp: boolean;
}

/**
 * Returns the next value of ``userScrolledUp`` (true == do not follow the tail).
 * A user scrolling down past the end is clamped by the browser exactly onto the
 * bottom, so touch-to-reattach engages naturally from a wheel fling without the
 * user having to aim.
 */
export function nextUserScrolledUp(input: FollowStateInput): boolean {
  if (input.userMovedUp) {
    return true;
  }
  if (input.userMovedDown && input.isAtBottom && !input.hasMoreAfter) {
    return false;
  }
  return input.wasUserScrolledUp;
}

/**
 * Whether a live text selection should hold the transcript's virtualization and
 * eviction (so scrolling/streaming past the selected rows does not unmount them
 * and collapse the selection). DOM-free: the caller supplies the facts read from
 * ``document.getSelection()`` and a containment test against this view's scroll
 * element, so ChatPanel and SubagentView never react to each other's selections.
 */
export interface SelectionState {
  /** A selection object exists with at least one range. */
  hasRange: boolean;
  /** The selection is collapsed (a caret, no selected text). */
  isCollapsed: boolean;
  /** The anchor endpoint is inside this view's scroll element. */
  anchorWithin: boolean;
  /** The focus endpoint is inside this view's scroll element. */
  focusWithin: boolean;
}

export function isSelectionActiveWithin(state: SelectionState): boolean {
  // Either endpoint inside the view counts: a drag can start outside the panel
  // (anchor out, focus in) or be dragged out (anchor in, focus out), and in both
  // cases text inside this view is selected and must be protected.
  return state.hasRange && !state.isCollapsed && (state.anchorWithin || state.focusWithin);
}
