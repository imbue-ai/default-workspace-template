/**
 * The Usage widget in the taskbar's row, among the windows' entries (app-widgets plan sections 4.10 and 7.8): one of
 * four designs the workspace picks, each drawn to sit in the bar as an entry does -- as tall as one, its names in the
 * bar's own text rather than the tray's small capitals. How much it shows follows the detail pick: with less, the
 * 5-hour limit alone; with more, the weekly figure in view too. Every mark's tooltip says its window, the share used
 * and when it resets, and a click opens the widget expanded above the bar.
 *
 * - Chip: like a window's entry, the ring pair as its icon and the name and 5-hour figure as its title.
 * - Meters: upright 5-hour and weekly meters, as tall as the entry, beside the name over the 5-hour figure.
 * - Inline: the names and 5-hour figures as one line of text, like a window's title.
 * - Line: the name and 5-hour figure over thin lines filled to each window's share.
 */

import m from "mithril";
import type { LimitWindow } from "../models/providers";
import { ringFraction, windowText } from "../models/widget";
import type { RingPair, WidgetModel } from "../models/widget";
import type { WidgetDetail, WidgetToolbar } from "../models/widgetLook";
import { figure, perSubscription, tooltip, windowColour } from "./widgetParts";
import type { WindowRole } from "./widgetParts";
import { ringsView } from "./widgetRings";

/** The weekly window, unless the detail pick leaves it out. */
function weeklyOf(pair: RingPair, detail: WidgetDetail): LimitWindow | null {
  return detail === "less" ? null : pair.outer;
}

function name(pair: RingPair, colour = "text-(--widget-text)"): m.Vnode {
  return m(
    "span",
    { "data-ring-name": "", class: `whitespace-nowrap ${colour}`, ...tooltip(pair.tooltip) },
    pair.name,
  );
}

/** The 5-hour figure, and with more the weekly one after it. */
function figures(pair: RingPair, detail: WidgetDetail): m.Children {
  return [
    figure(pair.inner, "short", "font-semibold"),
    detail === "more" && pair.outer !== null ? figure(pair.outer, "long", "text-(--widget-muted)") : null,
  ];
}

function chip(model: WidgetModel, detail: WidgetDetail): m.Children {
  return perSubscription(model, (pair) =>
    m("span", { key: pair.key, "data-widget-rings": pair.key, class: "flex items-center gap-1.5" }, [
      ringsView(pair.inner, weeklyOf(pair, detail), pair.tooltip, false),
      name(pair),
      figures(pair, detail),
    ]),
  );
}

/** One upright meter, filled from the bottom to the window's share. */
function meter(window: LimitWindow, role: WindowRole): m.Vnode {
  return m(
    "span",
    {
      "data-widget-meter": role,
      class: `flex h-full flex-col justify-end overflow-hidden rounded-sm bg-(--widget-track) ${
        role === "short" ? "w-1.5" : "w-1"
      }`,
      ...tooltip(windowText(window)),
    },
    m("span", {
      "data-widget-bar-level": window.level,
      class: "block w-full",
      style: { height: `${ringFraction(window) * 100}%`, background: windowColour(window, role) },
    }),
  );
}

function meters(model: WidgetModel, detail: WidgetDetail): m.Children {
  return perSubscription(model, (pair) => {
    const weekly = weeklyOf(pair, detail);
    return m("span", { key: pair.key, "data-widget-rings": pair.key, class: "flex h-full items-center gap-1.5" }, [
      m("span", { class: "flex h-(--w-meter) items-stretch gap-0.5" }, [
        meter(pair.inner, "short"),
        weekly === null ? null : meter(weekly, "long"),
      ]),
      m("span", { class: "flex flex-col leading-tight" }, [
        m("span", { class: "text-[0.85em]" }, name(pair, "text-(--widget-muted)")),
        m("span", { class: "flex items-baseline gap-1" }, figures(pair, detail)),
      ]),
    ]);
  });
}

function inline(model: WidgetModel, detail: WidgetDetail): m.Children {
  return perSubscription(model, (pair) =>
    m(
      "span",
      {
        key: pair.key,
        "data-widget-rings": pair.key,
        // A middle dot before every subscription but the first, as a title's parts are joined.
        class:
          "flex items-baseline gap-1 whitespace-nowrap [&+&]:before:text-(--widget-faint) [&+&]:before:content-['·']",
      },
      [name(pair), figures(pair, detail)],
    ),
  );
}

/** A thin line filled to the window's share. */
function line(window: LimitWindow, role: WindowRole): m.Vnode {
  return m(
    "span",
    {
      "data-widget-bar-line": role,
      class: `block w-full overflow-hidden rounded-full bg-(--widget-track) ${role === "short" ? "h-[3px]" : "h-[2px]"}`,
      ...tooltip(windowText(window)),
    },
    m("span", {
      "data-widget-bar": role,
      "data-widget-bar-level": window.level,
      class: "block h-full rounded-full",
      style: { width: `${ringFraction(window) * 100}%`, background: windowColour(window, role) },
    }),
  );
}

function lines(model: WidgetModel, detail: WidgetDetail): m.Children {
  return perSubscription(model, (pair) => {
    const weekly = weeklyOf(pair, detail);
    return m(
      "span",
      { key: pair.key, "data-widget-rings": pair.key, class: "flex min-w-[5.5em] flex-col gap-[3px]" },
      [
        m("span", { class: "flex items-baseline justify-between gap-2 leading-none" }, [
          name(pair),
          m("span", { class: "flex items-baseline gap-1" }, figures(pair, detail)),
        ]),
        line(pair.inner, "short"),
        weekly === null ? null : line(weekly, "long"),
      ],
    );
  });
}

export const TOOLBAR_DESIGNS: Record<WidgetToolbar, (model: WidgetModel, detail: WidgetDetail) => m.Children> = {
  chip,
  meters,
  inline,
  line: lines,
};
