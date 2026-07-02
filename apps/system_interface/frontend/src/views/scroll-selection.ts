/**
 * DOM glue for scroll anchoring and text-selection preservation, shared by the
 * main chat panel and the subagent view. Both virtualize their transcript into a
 * windowed `.message-list`, and both need the same two things the pure models
 * (virtualWindow, scrollFollow) can't do because they touch the live DOM:
 *
 *  - anchor the viewport to a specific row so content growing/shrinking *above*
 *    it (a backfill prepend, an off-screen row measuring taller) doesn't shift
 *    what the user is reading, and
 *  - find which rows a live text selection touches, so the window can keep those
 *    rows mounted (removing a selection endpoint's node collapses the selection).
 *
 * Every message row's root element carries a DOM `id` equal to its virtualization
 * key (see message-renderers / conversation-rows); spacers have an empty id.
 */

import { type SelectionState } from "../models/scrollFollow";

// Stop holding a selection's rows in the virtualization window once the viewport
// is more than this many rows away from them, so a selection left active during a
// long stream can't keep an unbounded span of rows mounted. Past this the pin is
// dropped (and the selection collapses) -- a deliberate memory bound; in practice
// users select-then-copy within seconds, far inside this gap.
export const SELECTION_PIN_MAX_GAP_ROWS = 300;

/** The top of `el` expressed in `scrollEl`'s scroll-content coordinates (i.e.
 *  independent of the current scrollTop), so it can be compared across reflows. */
function contentTopWithin(el: HTMLElement, scrollEl: HTMLElement): number {
  return el.getBoundingClientRect().top - scrollEl.getBoundingClientRect().top + scrollEl.scrollTop;
}

/** The rendered message rows (children of `.message-list` carrying a non-empty
 *  id), in document order. Empty when the list isn't mounted yet. */
function rowElements(scrollEl: HTMLElement): HTMLElement[] {
  const list = scrollEl.querySelector(".message-list");
  if (list === null) {
    return [];
  }
  const rows: HTMLElement[] = [];
  for (const child of Array.from(list.children)) {
    const el = child as HTMLElement;
    if (el.id !== "") {
      rows.push(el);
    }
  }
  return rows;
}

export interface ScrollAnchor {
  /** The anchored row's virtualization key (its DOM id). */
  key: string;
  /** Where the row's top sat in scroll-content coordinates when captured. */
  contentTop: number;
}

/**
 * Capture the topmost rendered row currently intersecting the viewport top, to
 * anchor the viewport against later height changes above it. Null when nothing is
 * rendered (the caller then leaves the previous anchor in place / re-captures).
 */
export function captureTopAnchor(scrollEl: HTMLElement): ScrollAnchor | null {
  const scrollTop = scrollEl.scrollTop;
  for (const el of rowElements(scrollEl)) {
    const top = contentTopWithin(el, scrollEl);
    if (top + el.offsetHeight > scrollTop) {
      return { key: el.id, contentTop: top };
    }
  }
  return null;
}

/** The current scroll-content top of the row with this key, or null if it isn't
 *  mounted (re-keyed, evicted, or scrolled out of the window). */
export function contentTopOfRow(scrollEl: HTMLElement, key: string): number | null {
  for (const el of rowElements(scrollEl)) {
    if (el.id === key) {
      return contentTopWithin(el, scrollEl);
    }
  }
  return null;
}

/** Walk up from a selection endpoint node to the message-row element (the child
 *  of `.message-list`) and return its key, or null if the node isn't inside a
 *  row. */
function rowKeyForNode(node: Node | null, listEl: Element): string | null {
  let current: Node | null = node;
  while (current !== null && current !== listEl) {
    const parent = current.parentNode;
    if (parent === listEl && current instanceof HTMLElement && current.id !== "") {
      return current.id;
    }
    current = parent;
  }
  return null;
}

/** Read the current selection's facts relative to this view's scroll element, for
 *  the pure `isSelectionActiveWithin` decision. */
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

/**
 * The inclusive row-index range spanned by the live selection's endpoints within
 * this view, or null when there is no active selection here or its endpoints
 * don't map to known rows (e.g. a selection anchored above `.message-list`, like
 * Cmd+A). Used to pin those rows into the virtualization window.
 */
export function resolveSelectionRowRange(
  scrollEl: HTMLElement | null,
  keyToIndex: Map<string, number>,
): { start: number; end: number } | null {
  if (scrollEl === null) {
    return null;
  }
  const selection = document.getSelection();
  if (selection === null || selection.rangeCount === 0 || selection.isCollapsed) {
    return null;
  }
  const list = scrollEl.querySelector(".message-list");
  if (list === null) {
    return null;
  }
  const indices: number[] = [];
  for (const node of [selection.anchorNode, selection.focusNode]) {
    if (node === null || !scrollEl.contains(node)) {
      continue;
    }
    const key = rowKeyForNode(node, list);
    if (key === null) {
      continue;
    }
    const index = keyToIndex.get(key);
    if (index !== undefined) {
      indices.push(index);
    }
  }
  if (indices.length === 0) {
    return null;
  }
  return { start: Math.min(...indices), end: Math.max(...indices) };
}
