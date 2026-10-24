/**
 * The phone's widgets (app-widgets plan section 4.8): a strip just above the bar, holding each enabled widget whose
 * page has reported a size and not asked to be hidden, always collapsed and no taller than the phone's widget height.
 * The phone has no floating layer, so there is no rim, menu, or place to move it to: a tap is the page's, and its link
 * opens the app's window, which the phone shows. Each widget is only the box its page is laid over; the widget frames
 * layer lays it there.
 */

import m from "mithril";
import { clampedWidgetSize } from "../../geometry/widgets";
import type { WidgetMetrics } from "../../geometry/widgets";
import { WIDGET_ATTRIBUTE, WIDGET_CONTENT_ATTRIBUTE } from "../../pages/widgetFrames";
import type { ShownWidget, WidgetReport } from "../../reducers/desktopState";

export interface PhoneWidgetsAttrs {
  readonly widgets: readonly ShownWidget[];
  readonly reports: Readonly<Record<string, WidgetReport>>;
  readonly metrics: WidgetMetrics;
}

export const PhoneWidgets: m.Component<PhoneWidgetsAttrs> = {
  view(vnode) {
    const { widgets, reports, metrics } = vnode.attrs;
    const drawn = widgets.flatMap(({ app }) => {
      const report = reports[app.name];
      if (report === undefined || report.size === null || report.isHidden) return [];
      return [{ app, size: clampedWidgetSize(report.size, metrics) }];
    });
    if (drawn.length === 0) return null;
    return m(
      "div",
      {
        "data-phone-widgets": "",
        class:
          "phone-widgets flex shrink-0 items-center justify-center gap-2 overflow-x-auto border-t border-default " +
          "bg-page/90 px-(--desk-phone-bar-padding) pt-(--desk-phone-bar-padding) backdrop-blur-xl",
      },
      drawn.map(({ app, size }) =>
        m(
          "div",
          {
            key: app.name,
            [WIDGET_ATTRIBUTE]: app.name,
            "data-phone-widget": app.name,
            class:
              "phone-widget flex max-h-(--desk-phone-widget-height) shrink-0 overflow-hidden " +
              "rounded-(--desk-widget-radius) border border-(--desk-widget-edge) bg-(--desk-widget-surface)",
            style: { width: `${size.width}px`, height: `${size.height}px` },
          },
          m("div", { [WIDGET_CONTENT_ATTRIBUTE]: "", class: "phone-widget-content min-w-0 flex-1" }),
        ),
      ),
    );
  },
};
