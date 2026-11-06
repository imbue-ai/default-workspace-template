/**
 * The stacking order of the windows layer: a window's live page and its chrome (or the ghost standing in for
 * it) are interleaved by stack index, the page at 2i+1 and the chrome at 2i+2, so the chrome sits over its own
 * page and over every lower window's page and chrome.
 */

/** The z-index of the live page of the window at ``stackIndex``. */
export function windowPageZIndex(stackIndex: number): string {
  return String(2 * stackIndex + 1);
}

/** The z-index of the chrome (or the ghost) of the window at ``stackIndex``. */
export function windowChromeZIndex(stackIndex: number): string {
  return String(2 * stackIndex + 2);
}

/** The z-index of the overview's scrim: the sticky layer, over every window, under the menus and cards. */
export const OVERVIEW_SCRIM_Z_INDEX = "var(--z-sticky)";

/** The z-index of a live page laid over an overview tile: just above the scrim. */
export const OVERVIEW_PAGE_Z_INDEX = `calc(${OVERVIEW_SCRIM_Z_INDEX} + 1)`;
