/**
 * Keyboard-aware viewport height sync.
 *
 * The app is sized with 100dvh (see theme.css), which tracks the mobile URL
 * bar but NOT the on-screen keyboard: on iOS Safari the keyboard overlays the
 * layout viewport without resizing it, leaving the composer hidden behind the
 * keyboard while typing. window.visualViewport does shrink for the keyboard,
 * so while it is meaningfully smaller than the layout viewport this module
 * clamps the app to it: it sets --app-viewport-height and the
 * .app-viewport-clamped class on <html> (theme.css applies the height only
 * under that class). When the keyboard closes, both are removed and the dvh
 * sizing is back in charge.
 *
 * While clamped, the window is also pinned to scroll position 0: focusing an
 * input makes iOS pan the (unresized) layout viewport to reveal the caret,
 * which would otherwise leave the app shell half scrolled out of view.
 */

// The visual viewport can run a hair smaller than the layout viewport from
// fractional device-pixel rounding, and shrinks slightly for minor browser UI.
// Only clamp when the gap is large enough that it can only mean an on-screen
// keyboard.
const MIN_VIEWPORT_GAP_PX = 50;

/** The app height (px) to clamp to, or null for "leave dvh in charge".
 *  Pure decision logic, exercised directly by tests. */
export function computeViewportClamp(layoutViewportHeight: number, visualViewportHeight: number): number | null {
  const gap = layoutViewportHeight - visualViewportHeight;
  if (gap > MIN_VIEWPORT_GAP_PX) {
    return Math.round(visualViewportHeight);
  }
  return null;
}

function syncViewportHeight(): void {
  const viewport = window.visualViewport;
  if (viewport === null) {
    return;
  }
  const root = document.documentElement;
  const clampPx = computeViewportClamp(window.innerHeight, viewport.height);
  if (clampPx !== null) {
    root.style.setProperty("--app-viewport-height", `${clampPx}px`);
    root.classList.add("app-viewport-clamped");
    if (window.scrollY !== 0) {
      window.scrollTo(0, 0);
    }
  } else {
    root.style.removeProperty("--app-viewport-height");
    root.classList.remove("app-viewport-clamped");
  }
}

export function initViewportHeightSync(): void {
  const viewport = window.visualViewport;
  if (viewport === null) {
    return;
  }
  viewport.addEventListener("resize", syncViewportHeight);
  viewport.addEventListener("scroll", syncViewportHeight);
  syncViewportHeight();
}
