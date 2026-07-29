/**
 * Where to put the fast-mode prompt so it points at the control it is about.
 *
 * The prompt asks the same question the composer's lightning-bolt toggle
 * answers, so it is shown as a popover just above that button rather than
 * floating in the middle of the screen. The geometry lives here, apart from the
 * view, because it is pure: the modal measures the button and the window and
 * passes the rects in (see FastModeModal.ts).
 */

export interface AnchorRect {
  left: number;
  top: number;
  width: number;
}

export interface ViewportSize {
  width: number;
  height: number;
}

export interface AnchoredPosition {
  /** Viewport-relative offsets, in pixels, for a `position: fixed` box. */
  left: number;
  bottom: number;
  width: number;
  /** Where the arrow sits along the popover's bottom edge, from its left edge. */
  arrowLeft: number;
}

/** Popover width wherever the viewport has room for it (matches style.css). */
export const FAST_MODE_MODAL_WIDTH_PX = 460;
/** Kept clear of the viewport edges so the popover never sits flush against one. */
const VIEWPORT_MARGIN_PX = 12;
/** Space left between the popover's bottom edge and the toggle it points at. */
const TOGGLE_GAP_PX = 12;
/** With less room than this above the toggle the popover would not fit above it. */
const REQUIRED_ROOM_ABOVE_PX = 300;
/** Keeps the arrow clear of the popover's rounded corners. */
const ARROW_INSET_PX = 20;

/**
 * Place the popover above `rect`, or return null to leave it centered.
 *
 * Centering is the fallback for every case where anchoring would look wrong: a
 * toggle in a hidden panel (which measures as a zero-width rect), one scrolled
 * out of view, or one with too little room above it for the popover.
 */
export function computeAnchoredPosition(rect: AnchorRect, viewport: ViewportSize): AnchoredPosition | null {
  const width = Math.min(FAST_MODE_MODAL_WIDTH_PX, viewport.width - 2 * VIEWPORT_MARGIN_PX);
  if (rect.width <= 0 || width <= 0) {
    return null;
  }
  if (rect.top < REQUIRED_ROOM_ABOVE_PX || rect.top > viewport.height) {
    return null;
  }
  const toggleCenter = rect.left + rect.width / 2;
  const left = clamp(toggleCenter - width / 2, VIEWPORT_MARGIN_PX, viewport.width - width - VIEWPORT_MARGIN_PX);
  return {
    left,
    bottom: viewport.height - rect.top + TOGGLE_GAP_PX,
    width,
    arrowLeft: clamp(toggleCenter - left, ARROW_INSET_PX, width - ARROW_INSET_PX),
  };
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high);
}
