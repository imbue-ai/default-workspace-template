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
