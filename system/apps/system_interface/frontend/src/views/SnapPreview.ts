/**
 * The translucent preview of the zone a dragged window will snap to (plan section 4.3), drawn
 * over the backdrop while the pointer is inside the zone.
 */

import m from "mithril";
import type { PixelRect } from "../geometry/frames";

export interface SnapPreviewAttrs {
  readonly rect: PixelRect;
}

export const SnapPreview: m.Component<SnapPreviewAttrs> = {
  view(vnode) {
    const { rect } = vnode.attrs;
    return m("div", {
      "data-snap-preview": "",
      class:
        "snap-preview pointer-events-none absolute z-(--z-sticky) rounded-(--desk-window-radius) border-2 border-accent bg-accent/15",
      style: { left: `${rect.x}px`, top: `${rect.y}px`, width: `${rect.width}px`, height: `${rect.height}px` },
    });
  },
};
