/**
 * Frames (desktop-interface contracts.md section 10): the pure rules over a window's rectangle,
 * mirroring ``shell/desktop_document.py`` constant for constant. Both pass the shared vectors in
 * ``docs/system/blueprint/desktop-interface/geometry_vectors.json``. Nothing here reads the DOM.
 */

import type { Frame, WindowState } from "../model/records";

// The cascade rule (fixed, in fractions): window ``n`` of a client's desktop steps from the origin, cycling.
export const CASCADE_ORIGIN_X = 0.05;
export const CASCADE_ORIGIN_Y = 0.06;
export const CASCADE_STEP_X = 0.03;
export const CASCADE_STEP_Y = 0.04;
export const CASCADE_WIDTH = 0.6;
export const CASCADE_HEIGHT = 0.7;
export const CASCADE_CYCLE = 6;

export const SNAPPED_LEFT_FRAME: Frame = Object.freeze({ x: 0, y: 0, width: 0.5, height: 1 });
export const SNAPPED_RIGHT_FRAME: Frame = Object.freeze({ x: 0.5, y: 0, width: 0.5, height: 1 });
export const MAXIMIZED_FRAME: Frame = Object.freeze({ x: 0, y: 0, width: 1, height: 1 });

/** A size in pixels: the backdrop (the viewport less the taskbar), or a page's box. */
export interface PixelSize {
  readonly width: number;
  readonly height: number;
}

/** A rendered rectangle in backdrop pixels. */
export interface PixelRect {
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

export interface PixelPoint {
  readonly x: number;
  readonly y: number;
}

/** The theme metrics the fit rule reads. */
export interface FitMetrics {
  readonly windowMinWidth: number;
  readonly windowMinHeight: number;
  readonly titleMinVisible: number;
  readonly titleBarHeight: number;
}

function clampNumber(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high);
}

/** The frame shifted (and, past the square's size, shrunk) so that it lies wholly inside the unit square. */
export function clampFrameIntoUnitSquare(x: number, y: number, width: number, height: number): Frame {
  const clampedWidth = clampNumber(width, 0, 1);
  const clampedHeight = clampNumber(height, 0, 1);
  return {
    x: clampNumber(x, 0, 1 - clampedWidth),
    y: clampNumber(y, 0, 1 - clampedHeight),
    width: clampedWidth,
    height: clampedHeight,
  };
}

/** The frame the ``placedCount``-th window a client places on a desktop opens at (zero-based, cycling). */
export function cascadeFrame(placedCount: number): Frame {
  const step = placedCount % CASCADE_CYCLE;
  return clampFrameIntoUnitSquare(
    CASCADE_ORIGIN_X + CASCADE_STEP_X * step,
    CASCADE_ORIGIN_Y + CASCADE_STEP_Y * step,
    CASCADE_WIDTH,
    CASCADE_HEIGHT,
  );
}

/** The frame a window renders at: its own when normal, the fixed half or whole otherwise. */
export function frameForState(placementFrame: Frame, state: WindowState): Frame {
  switch (state) {
    case "NORMAL":
      return placementFrame;
    case "SNAPPED_LEFT":
      return SNAPPED_LEFT_FRAME;
    case "SNAPPED_RIGHT":
      return SNAPPED_RIGHT_FRAME;
    case "MAXIMIZED":
      return MAXIMIZED_FRAME;
  }
}

/** The fit rule (render only): fractions to pixels, the minimum size enforced, the title bar nudged into view. */
export function fitFrameToBackdrop(frame: Frame, backdrop: PixelSize, metrics: FitMetrics): PixelRect {
  const width = Math.max(frame.width * backdrop.width, metrics.windowMinWidth);
  const height = Math.max(frame.height * backdrop.height, metrics.windowMinHeight);
  const scaledX = frame.x * backdrop.width;
  const scaledY = frame.y * backdrop.height;
  // Horizontally at least the minimum visible title width stays inside, on either side.
  const visible = Math.min(metrics.titleMinVisible, width);
  const x = Math.min(Math.max(scaledX, visible - width), backdrop.width - visible);
  // Vertically the whole title bar stays inside, and the top edge is never above the backdrop's.
  const y = Math.max(Math.min(scaledY, backdrop.height - metrics.titleBarHeight), 0);
  return { x, y, width, height };
}

/** The state a drag released at the pointer snaps to, or null outside every zone; the top edge wins a corner. */
export function snapZoneForRelease(pointer: PixelPoint, backdrop: PixelSize, threshold: number): WindowState | null {
  if (pointer.y <= threshold) return "MAXIMIZED";
  if (pointer.x <= threshold) return "SNAPPED_LEFT";
  if (pointer.x >= backdrop.width - threshold) return "SNAPPED_RIGHT";
  return null;
}

/** The un-snap rule: the kept frame's size, hung so the pointer (in fractions of the backdrop) sits at
 *  ``grabFraction`` across the title bar and ``grabOffsetY`` below the window's top, then clamped. */
export function unsnapFrame(keptFrame: Frame, pointer: PixelPoint, grabFraction: number, grabOffsetY: number): Frame {
  return clampFrameIntoUnitSquare(
    pointer.x - grabFraction * keptFrame.width,
    pointer.y - grabOffsetY,
    keptFrame.width,
    keptFrame.height,
  );
}

/** A pixel rectangle over the backdrop as a frame in fractions, clamped into the unit square. */
export function frameFromPixels(rect: PixelRect, backdrop: PixelSize): Frame {
  if (backdrop.width <= 0 || backdrop.height <= 0) return clampFrameIntoUnitSquare(0, 0, 1, 1);
  return clampFrameIntoUnitSquare(
    rect.x / backdrop.width,
    rect.y / backdrop.height,
    rect.width / backdrop.width,
    rect.height / backdrop.height,
  );
}

/** The frame's rectangle in backdrop pixels, with no fit applied. */
export function frameToPixels(frame: Frame, backdrop: PixelSize): PixelRect {
  return {
    x: frame.x * backdrop.width,
    y: frame.y * backdrop.height,
    width: frame.width * backdrop.width,
    height: frame.height * backdrop.height,
  };
}

export type ResizeEdge = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

export const RESIZE_EDGES: readonly ResizeEdge[] = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];

/** Whether a string names one of the eight resize edges. */
export function isResizeEdge(value: string): value is ResizeEdge {
  return (RESIZE_EDGES as readonly string[]).includes(value);
}

/**
 * The rectangle a resize by ``edge`` reaches: the grabbed edges follow the pointer's delta, the
 * others stay, and the minimum size is held from the moving edge so the anchored edge never moves.
 */
export function resizedRect(
  start: PixelRect,
  edge: ResizeEdge,
  delta: PixelPoint,
  minimum: Pick<FitMetrics, "windowMinWidth" | "windowMinHeight">,
): PixelRect {
  let { x, y, width, height } = start;
  if (edge.includes("e")) width = Math.max(minimum.windowMinWidth, start.width + delta.x);
  if (edge.includes("s")) height = Math.max(minimum.windowMinHeight, start.height + delta.y);
  if (edge.includes("w")) {
    width = Math.max(minimum.windowMinWidth, start.width - delta.x);
    x = start.x + start.width - width;
  }
  if (edge.includes("n")) {
    height = Math.max(minimum.windowMinHeight, start.height - delta.y);
    y = start.y + start.height - height;
  }
  return { x, y, width, height };
}

/** The rectangle a move by ``delta`` reaches, clamped so at least ``titleMinVisible`` of the title bar
 *  stays inside horizontally and the top edge stays inside the backdrop. */
export function movedRect(
  start: PixelRect,
  delta: PixelPoint,
  backdrop: PixelSize,
  metrics: Pick<FitMetrics, "titleMinVisible" | "titleBarHeight">,
): PixelRect {
  const visible = Math.min(metrics.titleMinVisible, start.width);
  const x = Math.min(Math.max(start.x + delta.x, visible - start.width), backdrop.width - visible);
  const y = Math.max(Math.min(start.y + delta.y, backdrop.height - metrics.titleBarHeight), 0);
  return { x, y, width: start.width, height: start.height };
}
