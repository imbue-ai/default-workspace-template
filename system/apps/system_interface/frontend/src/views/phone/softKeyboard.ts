/**
 * Whether a phone's soft keyboard is up, read off the viewports. No browser says so outright on iOS: iOS Safari (and
 * Chrome 108+ and Firefox 132+ on Android, by default) shrinks only the visual viewport for the keyboard and leaves
 * the layout viewport alone, so a keyboard shows as a visual viewport as wide as the layout and much shorter.
 *
 * Every condition is required, so nothing else reads as a keyboard:
 * - a coarse primary pointer: a desktop with a mouse never qualifies, at any window size;
 * - the shell's focus on a text field or an app's frame: app pages are cross-origin, so a field focused in one
 *   shows here only as its frame;
 * - the visual viewport as wide as the layout: a pinch zoom narrows it, a keyboard does not;
 * - the visual viewport shorter than the layout by more than a fifth: past a status bar's difference or an iPad's
 *   hardware-keyboard shortcut bar.
 * Focus alone is not enough: a keyboard dismissed with iOS's Done, or a hardware keyboard, leaves the field focused.
 * Once up, the keyboard counts as up until the visual viewport grows back, whatever holds the focus: a tap that moves
 * the focus off the field leaves the keyboard on screen for its hide animation.
 */

import { TOUCH_MEDIA_QUERY } from "@imbue/workspace-ui/src/device_queries";
import { isTextEditingTarget } from "@imbue/workspace-ui/src/hideCursorWhileTyping";

const MIN_KEYBOARD_SHARE = 0.2;
const WIDTH_TOLERANCE_PX = 2;

export interface ViewportReading {
  readonly isCoarsePointer: boolean;
  readonly isTypingFocus: boolean;
  readonly layoutWidthPx: number;
  readonly layoutHeightPx: number;
  readonly visualWidthPx: number;
  readonly visualHeightPx: number;
}

/** Whether focus on ``element`` can have raised a soft keyboard: a writable text field, or an app's frame. */
export function isTypingFocus(element: Element | null): boolean {
  return element instanceof HTMLIFrameElement || isTextEditingTarget(element);
}

export function isSoftKeyboardUp(reading: ViewportReading, wasUp: boolean): boolean {
  if (!reading.isCoarsePointer || !(reading.isTypingFocus || wasUp)) return false;
  if (Math.abs(reading.visualWidthPx - reading.layoutWidthPx) >= WIDTH_TOLERANCE_PX) return false;
  return reading.layoutHeightPx - reading.visualHeightPx > reading.layoutHeightPx * MIN_KEYBOARD_SHARE;
}

/** The reading for this page now, or null where the browser has no visual viewport to read. */
export function readViewport(): ViewportReading | null {
  const viewport = window.visualViewport;
  if (!viewport) return null;
  return {
    isCoarsePointer: window.matchMedia(TOUCH_MEDIA_QUERY).matches,
    isTypingFocus: isTypingFocus(document.activeElement),
    layoutWidthPx: document.documentElement.clientWidth,
    layoutHeightPx: document.documentElement.clientHeight,
    visualWidthPx: viewport.width,
    visualHeightPx: viewport.height,
  };
}
