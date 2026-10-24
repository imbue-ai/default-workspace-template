/**
 * The Usage widget in the taskbar's tray (app-widgets plan sections 4.10 and 7.8): one of five designs the workspace
 * picks, each a mark per subscription within the tray's height, whatever size was picked for the desktop. How much it
 * shows follows the detail pick: with less, the 5-hour limit alone. Every mark's tooltip says its window, the share
 * used and when it resets, and a click opens the widget expanded above the bar.
 *
 * - Rings: a small ring pair beside the name over the 5-hour figure (``widgetRings.ts``).
 * - Ring only: the ring pair alone, like a menu-bar icon.
 * - Bars: the name over the 5-hour and weekly bars, beside the 5-hour figure (``widgetBars.ts``).
 * - Numbers: the name and the 5-hour figure as text, the weekly figure after it with more.
 * - Stacked: the name beside the 5-hour figure over the weekly one, each with its colour's dot.
 */

import m from "mithril";
import type { LimitWindow } from "../models/providers";
import type { WidgetModel } from "../models/widget";
import type { WidgetDetail, WidgetTray } from "../models/widgetLook";
import { barsTray } from "./widgetBars";
import { TRAY_NAME_CLASS, figure, perSubscription, tooltip, windowColour } from "./widgetParts";
import type { WindowRole } from "./widgetParts";
import { ringsTray, ringsView } from "./widgetRings";

function ringOnly(model: WidgetModel, detail: WidgetDetail): m.Children {
  return perSubscription(model, (pair) =>
    m("span", { key: pair.key, "data-widget-rings": pair.key, class: "flex items-center" }, [
      ringsView(pair.inner, detail === "less" ? null : pair.outer, pair.tooltip, false),
    ]),
  );
}

function numbers(model: WidgetModel, detail: WidgetDetail): m.Children {
  return perSubscription(model, (pair) =>
    m("span", { key: pair.key, "data-widget-rings": pair.key, class: "flex items-baseline gap-1 whitespace-nowrap" }, [
      m("span", { "data-ring-name": "", class: TRAY_NAME_CLASS, ...tooltip(pair.tooltip) }, pair.name),
      figure(pair.inner, "short", "font-semibold"),
      detail === "more" && pair.outer !== null
        ? figure(pair.outer, "long", "text-[0.82em] text-(--widget-muted)")
        : null,
    ]),
  );
}

/** One line of the stacked design: the window's dot and figure. */
function stackedLine(window: LimitWindow, role: WindowRole): m.Vnode {
  return m("span", { "data-widget-window": window.key, class: "flex items-center gap-1 leading-none" }, [
    m("span", { class: "inline-block size-1.5 rounded-full", style: { background: windowColour(window, role) } }),
    figure(window, role, role === "short" ? "font-semibold" : "text-(--widget-muted)"),
  ]);
}

function stacked(model: WidgetModel, detail: WidgetDetail): m.Children {
  return perSubscription(model, (pair) =>
    m("span", { key: pair.key, "data-widget-rings": pair.key, class: "flex items-center gap-1.5" }, [
      m("span", { "data-ring-name": "", class: TRAY_NAME_CLASS, ...tooltip(pair.tooltip) }, pair.name),
      m("span", { class: "flex flex-col gap-1" }, [
        stackedLine(pair.inner, "short"),
        detail === "less" || pair.outer === null ? null : stackedLine(pair.outer, "long"),
      ]),
    ]),
  );
}

export const TRAY_DESIGNS: Record<WidgetTray, (model: WidgetModel, detail: WidgetDetail) => m.Children> = {
  rings: ringsTray,
  ring: ringOnly,
  bars: barsTray,
  numbers,
  stacked,
};
