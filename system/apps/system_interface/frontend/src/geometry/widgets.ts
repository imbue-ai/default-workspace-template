/**
 * Where widgets are drawn (app-widgets plan sections 3.4, 4.2 and 4.4). A widget a client has not moved sits in
 * the default stack in the top-right corner, in the order the shell lists them, inset from the backdrop's edges
 * (which end above the taskbar), separated by the theme's gap, and starting below a window title bar's height so a
 * maximized window's controls stay clear of it. A widget the client moved sits wherever it was let go: its stored
 * point is the corner of its box nearest the backdrop's corner of the quadrant the point is in, so it grows away
 * from the edges it sits by when its page reports a new size, and it is kept wholly inside the backdrop. A
 * widget's box is the size its page last reported, clamped to the theme's widget minimum and maximum, inside the
 * rim it is dragged by. Nothing here reads the DOM.
 */

import type { WidgetPosition } from "../model/records";
import type { PixelRect, PixelSize } from "./frames";

/** The theme metrics a widget's geometry reads. */
export interface WidgetMetrics {
  readonly titleBarHeight: number;
  readonly widgetInset: number;
  readonly widgetGap: number;
  readonly widgetMaxWidth: number;
  readonly widgetMaxHeight: number;
  readonly widgetMinSize: number;
  readonly widgetRim: number;
}

/** One widget to place: its app, where it was moved to (null for the default stack), and the size its page
 *  reported. */
export interface WidgetToPlace {
  readonly app: string;
  readonly position: WidgetPosition | null;
  readonly size: PixelSize;
}

function clampNumber(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), Math.max(low, high));
}

/** The size a page's report is drawn at: clamped to the theme's minimum and maximum. */
export function clampedWidgetSize(reported: PixelSize, metrics: WidgetMetrics): PixelSize {
  return {
    width: clampNumber(reported.width, metrics.widgetMinSize, metrics.widgetMaxWidth),
    height: clampNumber(reported.height, metrics.widgetMinSize, metrics.widgetMaxHeight),
  };
}

/** The size of a widget's box: its page's report, clamped, inside the rim. */
function boxSize(reported: PixelSize, metrics: WidgetMetrics): PixelSize {
  const content = clampedWidgetSize(reported, metrics);
  return { width: content.width + 2 * metrics.widgetRim, height: content.height + 2 * metrics.widgetRim };
}

/** The box of a widget moved to ``position``: anchored by the corner its point names, kept inside the backdrop. */
function positionedRect(position: WidgetPosition, size: PixelSize, backdrop: PixelSize): PixelRect {
  const anchorX = position.x * backdrop.width;
  const anchorY = position.y * backdrop.height;
  const x = position.x >= 0.5 ? anchorX - size.width : anchorX;
  const y = position.y >= 0.5 ? anchorY - size.height : anchorY;
  return {
    x: clampNumber(x, 0, backdrop.width - size.width),
    y: clampNumber(y, 0, backdrop.height - size.height),
    width: size.width,
    height: size.height,
  };
}

/** Every widget's box, in backdrop pixels: the unmoved ones stacked down from the top-right corner in the order
 *  given, each moved one where it was let go. */
export function widgetRects(
  widgets: readonly WidgetToPlace[],
  backdrop: PixelSize,
  metrics: WidgetMetrics,
): Map<string, PixelRect> {
  const rects = new Map<string, PixelRect>();
  let stacked = 0;
  for (const widget of widgets) {
    const size = boxSize(widget.size, metrics);
    if (widget.position !== null) {
      rects.set(widget.app, positionedRect(widget.position, size, backdrop));
      continue;
    }
    const x = backdrop.width - metrics.widgetInset - size.width;
    const y = metrics.titleBarHeight + metrics.widgetInset + stacked;
    rects.set(widget.app, { x, y, ...size });
    stacked += size.height + metrics.widgetGap;
  }
  return rects;
}

/** The position a widget let go at ``rect`` is stored as: the corner of its box nearest the backdrop's corner of
 *  the quadrant its centre is in, in fractions of the backdrop. That corner lies in the same quadrant, so reading
 *  the position back anchors the box by the same corner. */
export function widgetPositionOf(rect: PixelRect, backdrop: PixelSize): WidgetPosition {
  if (backdrop.width <= 0 || backdrop.height <= 0) return { x: 1, y: 0 };
  const isRight = rect.x + rect.width / 2 >= backdrop.width / 2;
  const isLower = rect.y + rect.height / 2 >= backdrop.height / 2;
  const x = isRight ? rect.x + rect.width : rect.x;
  const y = isLower ? rect.y + rect.height : rect.y;
  return {
    x: clampNumber(x / backdrop.width, 0, 1),
    y: clampNumber(y / backdrop.height, 0, 1),
  };
}
