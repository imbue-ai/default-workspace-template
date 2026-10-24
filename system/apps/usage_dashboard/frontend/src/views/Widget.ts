/**
 * The Usage widget's view (app-widgets plan section 7), drawn on the shell's light widget panel in the manner of a
 * system monitor: the whole widget is one link to the Usage page, opened in a window of its own
 * (``target="_blank"``, which the shell's link rule turns into raising the Usage window). It draws the look the
 * workspace picked: its style (``widgetRings.ts``, ``widgetBars.ts``), its size (the ``--w-*`` lengths ``style.css``
 * gives each size) and how much it shows; in the taskbar's tray its tray design (``widgetTray.ts``), and in the bar's
 * row its toolbar design (``widgetToolbar.ts``). Whatever the look, while the collector indexes its first history the
 * widget says so rather than show partial totals, and while the stream is down it keeps the last figures, dimmed.
 * Nothing in it changes under the pointer; the detail is in tooltips the shell shows.
 */

import m from "mithril";
import type { WidgetMode } from "@imbue/workspace-ui/src/app_contract";
import type { WidgetModel } from "../models/widget";
import type { WidgetDetail, WidgetLook, WidgetVariant } from "../models/widgetLook";
import { barsCollapsed, barsExpanded } from "./widgetBars";
import { ringsCollapsed, ringsExpanded, ringsMinimal } from "./widgetRings";
import { TOOLBAR_DESIGNS } from "./widgetToolbar";
import { TRAY_DESIGNS } from "./widgetTray";

type DesktopMode = Exclude<WidgetMode, "tray" | "toolbar">;

const STYLES: Record<WidgetVariant, Record<DesktopMode, (model: WidgetModel, detail: WidgetDetail) => m.Children>> = {
  rings: { expanded: ringsExpanded, collapsed: ringsCollapsed },
  minimal: { expanded: ringsMinimal, collapsed: ringsMinimal },
  bars: { expanded: barsExpanded, collapsed: barsCollapsed },
};

export interface WidgetAttrs {
  readonly model: WidgetModel;
  readonly mode: WidgetMode;
  readonly look: WidgetLook;
}

export const Widget: m.Component<WidgetAttrs> = {
  view(vnode) {
    const { model, mode, look } = vnode.attrs;
    const isCollapsed = mode !== "expanded";
    const content: m.Children = model.isCatchingUp
      ? m("span", { class: "text-(--widget-muted)" }, isCollapsed ? "..." : "Indexing usage...")
      : mode === "tray"
        ? TRAY_DESIGNS[look.tray](model, look.detail)
        : mode === "toolbar"
          ? TOOLBAR_DESIGNS[look.toolbar](model, look.detail)
          : STYLES[look.variant][mode](model, look.detail);
    return m(
      "a",
      {
        id: "usage-widget",
        href: "/",
        target: "_blank",
        "data-widget-mode": mode,
        "data-widget-variant": look.variant,
        "data-widget-size": look.size,
        "data-widget-detail": look.detail,
        "data-widget-tray": look.tray,
        "data-widget-toolbar": look.toolbar,
        "data-live": model.isLive ? "true" : "false",
        class:
          "usage-widget inline-flex text-(--widget-text) no-underline " +
          (mode === "tray"
            ? "flex-row items-center gap-2.5 px-1 "
            : mode === "toolbar"
              ? "flex-row items-center gap-3 px-2 "
              : isCollapsed
                ? "flex-col gap-1 p-1 "
                : look.variant === "minimal"
                  ? "flex-col gap-2 p-1.5 "
                  : "w-(--w-width) flex-col gap-2 p-1.5 ") +
          (model.isLive ? "" : "opacity-60"),
      },
      [
        content,
        model.isLive || isCollapsed
          ? null
          : m("span", { "data-reconnecting": "", class: "flex items-center gap-1 text-(--widget-faint)" }, [
              m("span", { class: "inline-block size-1.5 rounded-full bg-(--widget-faint)" }),
              "Reconnecting",
            ]),
      ],
    );
  },
};
