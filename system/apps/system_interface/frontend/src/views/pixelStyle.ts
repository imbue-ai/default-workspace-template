/**
 * The inline style that puts an element at a rectangle of backdrop pixels, spelled once for
 * the two writers that must agree: a view's render (the style attribute of ``Window`` and
 * ``SnapPreview``) and the per-move paint of a drag or resize, which writes the same
 * properties straight onto the element with no redraw.
 */

import type { PixelRect } from "../geometry/frames";

export interface RectStyle {
  readonly left: string;
  readonly top: string;
  readonly width: string;
  readonly height: string;
}

export function rectStyle(rect: PixelRect): RectStyle {
  return {
    left: `${rect.x}px`,
    top: `${rect.y}px`,
    width: `${rect.width}px`,
    height: `${rect.height}px`,
  };
}

/** Write a rectangle onto an element outside any redraw; a later render of the same rectangle changes nothing. */
export function applyRectStyle(element: HTMLElement, rect: PixelRect): void {
  Object.assign(element.style, rectStyle(rect));
}
