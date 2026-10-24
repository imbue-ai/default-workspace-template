/**
 * The widgets' chrome (app-widgets plan section 4): a layer above every window and live page, beside the floating
 * entries' at the theme's sticky level, drawing for each widget its page reported a size for a light panel at its
 * place, with no bar: the content box the widget frames layer lays the page on fills it but for a thin rim, and
 * the rim is the handle. A press on it drags the widget anywhere on the backdrop, a right-click or long
 * press opens its menu, and a double-click collapses or expands it. At rest a widget is see-through; under the
 * pointer, or with its menu open, it is not. The layer is inert; only each widget's box takes a press. Nothing
 * here measures the DOM or listens for gestures.
 */

import m from "mithril";
import { targetElementOf } from "@imbue/workspace-ui/src/context_menu_rows";
import type { PixelRect } from "../geometry/frames";
import type { ShownWidget } from "../reducers/desktopState";
import { NO_DRAG_ATTRIBUTE, WIDGET_GRIP_ATTRIBUTE } from "../gestures/pointerGestures";
import { WIDGET_ATTRIBUTE, WIDGET_CONTENT_ATTRIBUTE } from "../pages/widgetFrames";
import { rectStyle } from "./pixelStyle";

export interface WidgetsAttrs {
  readonly widgets: readonly ShownWidget[];
  /** Where each drawn widget's box is, in backdrop pixels; a widget with none is not drawn. */
  readonly rects: ReadonlyMap<string, PixelRect>;
  readonly openMenuApp: string | null;
  readonly onToggleMode: (app: string) => void;
  readonly onContextMenu: (app: string, x: number, y: number, target: Element) => void;
}

export const Widgets: m.Component<WidgetsAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    return m(
      "div",
      { "data-widgets": "", class: "widgets pointer-events-none absolute inset-0 z-(--z-sticky)" },
      attrs.widgets.flatMap(({ app, presentation }): m.Children[] => {
        const rect = attrs.rects.get(app.name);
        if (rect === undefined) return [];
        const isMenuOpen = attrs.openMenuApp === app.name;
        return [
          m(
            "div",
            {
              key: app.name,
              [WIDGET_ATTRIBUTE]: app.name,
              [WIDGET_GRIP_ATTRIBUTE]: app.name,
              "aria-label": `${app.display_name} widget: drag its edge to move it, double-click to collapse or expand it`,
              "data-widget-moved": presentation.position === null ? "false" : "true",
              "data-widget-mode": presentation.mode,
              class:
                "widget pointer-events-auto absolute flex cursor-grab touch-none select-none overflow-hidden " +
                "rounded-(--desk-widget-radius) border border-(--desk-widget-edge) bg-(--desk-widget-surface) " +
                "p-(--desk-widget-rim) shadow-raised backdrop-blur-xl transition-opacity hover:opacity-100 " +
                (isMenuOpen ? "opacity-100 ring-2 ring-accent" : "opacity-(--desk-widget-rest-opacity)"),
              style: rectStyle(rect),
              ondblclick: () => attrs.onToggleMode(app.name),
              oncontextmenu: (event: MouseEvent) => {
                event.preventDefault();
                attrs.onContextMenu(app.name, event.clientX, event.clientY, targetElementOf(event));
              },
            },
            m("div", {
              [WIDGET_CONTENT_ATTRIBUTE]: "",
              [NO_DRAG_ATTRIBUTE]: "",
              class: "widget-content min-h-0 min-w-0 flex-1",
            }),
          ),
        ];
      }),
    );
  },
};
