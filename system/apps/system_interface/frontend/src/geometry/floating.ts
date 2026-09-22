/**
 * A floating entry's place (pinned-taskbar-entries plan section 3.4): a point in fractions of the
 * backdrop, the top-left corner of the entry's box, clamped at render time so the whole box stays
 * inside the backdrop (which ends above the taskbar), and defaulting to the bottom-right corner
 * inset by the theme's tokens. Nothing here reads the DOM.
 */

import type { FloatingPosition } from "../model/records";
import type { PixelPoint, PixelRect, PixelSize } from "./frames";

/** The theme metrics a floating entry's geometry reads. */
export interface FloatingEntryMetrics {
  readonly floatingEntrySize: number;
  readonly floatingEntryInsetX: number;
  readonly floatingEntryInsetY: number;
}

function clampNumber(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), Math.max(low, high));
}

/** The entry's box at ``position``, in backdrop pixels, shifted so it lies wholly inside the backdrop. */
export function floatingEntryRect(
  position: FloatingPosition,
  backdrop: PixelSize,
  metrics: FloatingEntryMetrics,
): PixelRect {
  const size = metrics.floatingEntrySize;
  return {
    x: clampNumber(position.x * backdrop.width, 0, backdrop.width - size),
    y: clampNumber(position.y * backdrop.height, 0, backdrop.height - size),
    width: size,
    height: size,
  };
}

/** The bottom-right corner, inset by the theme's tokens, as a position. */
export function defaultFloatingPosition(backdrop: PixelSize, metrics: FloatingEntryMetrics): FloatingPosition {
  if (backdrop.width <= 0 || backdrop.height <= 0) return { x: 0, y: 0 };
  return {
    x: clampNumber((backdrop.width - metrics.floatingEntryInsetX - metrics.floatingEntrySize) / backdrop.width, 0, 1),
    y: clampNumber(
      (backdrop.height - metrics.floatingEntryInsetY - metrics.floatingEntrySize) / backdrop.height,
      0,
      1,
    ),
  };
}

/** The position a box whose top-left corner is at ``corner`` (backdrop pixels) is stored as, clamped so the
 *  whole box is inside the backdrop, in fractions. */
export function floatingPositionFromPixels(
  corner: PixelPoint,
  backdrop: PixelSize,
  metrics: FloatingEntryMetrics,
): FloatingPosition {
  if (backdrop.width <= 0 || backdrop.height <= 0) return { x: 0, y: 0 };
  const size = metrics.floatingEntrySize;
  return {
    x: clampNumber(corner.x, 0, backdrop.width - size) / backdrop.width,
    y: clampNumber(corner.y, 0, backdrop.height - size) / backdrop.height,
  };
}
