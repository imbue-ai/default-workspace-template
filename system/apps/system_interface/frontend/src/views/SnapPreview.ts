/**
 * The translucent preview of the zone a dragged window will snap to (plan section 4.3), drawn
 * over the backdrop while the pointer is inside the zone. Always in the DOM and hidden when
 * there is no zone, so the drag can show and move it per pointer event without a redraw; the
 * style a render gives it and the style the drag writes are the same object, so a redraw from
 * any other cause mid-drag renders what the drag painted.
 */

import m from "mithril";
import type { PixelRect } from "../geometry/frames";
import { rectStyle } from "./pixelStyle";
import type { RectStyle } from "./pixelStyle";

export const SNAP_PREVIEW_ATTRIBUTE = "data-snap-preview";

export interface SnapPreviewAttrs {
  /** The zone's rectangle, or null when the drag is in no zone. */
  readonly rect: PixelRect | null;
}

/** Every style property the preview carries, so a hide clears the rectangle a show wrote. */
export interface SnapPreviewStyle extends RectStyle {
  readonly display: string;
}

export function snapPreviewStyle(rect: PixelRect | null): SnapPreviewStyle {
  if (rect === null) return { left: "", top: "", width: "", height: "", display: "none" };
  return { ...rectStyle(rect), display: "" };
}

/** Show the preview at the zone, or hide it, outside any redraw. */
export function applySnapPreviewStyle(element: HTMLElement, rect: PixelRect | null): void {
  Object.assign(element.style, snapPreviewStyle(rect));
}

export const SnapPreview: m.Component<SnapPreviewAttrs> = {
  view(vnode) {
    return m("div", {
      [SNAP_PREVIEW_ATTRIBUTE]: "",
      class:
        "snap-preview pointer-events-none absolute z-(--z-sticky) rounded-(--desk-window-radius) border-2 border-accent bg-accent/15",
      style: snapPreviewStyle(vnode.attrs.rect),
    });
  },
};
