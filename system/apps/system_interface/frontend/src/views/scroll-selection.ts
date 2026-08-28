/**
 * Text-selection facts for the transcript views. While a selection is live
 * inside a view's transcript, the scroll engine freezes row unmounting and
 * eviction (removing a selection endpoint's node collapses the selection).
 * The decision is DOM-free and unit-testable; `selectionStateWithin` supplies
 * the facts read from `document.getSelection()` with a containment test against
 * this view's scroll element, so ChatPanel and SubagentView never react to each
 * other's selections.
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

/** Read the current selection's facts relative to this view's scroll element. */
export function selectionStateWithin(scrollEl: HTMLElement | null): SelectionState {
  const inactive: SelectionState = { hasRange: false, isCollapsed: true, anchorWithin: false, focusWithin: false };
  if (scrollEl === null) {
    return inactive;
  }
  const selection = document.getSelection();
  if (selection === null || selection.rangeCount === 0) {
    return inactive;
  }
  return {
    hasRange: true,
    isCollapsed: selection.isCollapsed,
    anchorWithin: selection.anchorNode !== null && scrollEl.contains(selection.anchorNode),
    focusWithin: selection.focusNode !== null && scrollEl.contains(selection.focusNode),
  };
}
