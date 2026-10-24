/**
 * The bars version of the Usage widget (app-widgets plan section 7.8), after a system monitor's meters: a row per
 * subscription with its name, the 5-hour limit's bar above the weekly one's thinner bar, and their shares used, the
 * 5-hour one in bold. How much it shows follows the workspace's pick: with less, the 5-hour bar alone and no spend;
 * with more, each bar's reset time in view (expanded) or the weekly figure too (collapsed). Collapsed, the rows sit
 * under the subscription's short name and carry the 5-hour figure; in the taskbar's tray, the name sits over the bars
 * beside the 5-hour figure, within the tray's height. Nothing explains a bar in place: each bar's
 * tooltip says its window, the share used and when it resets, and the name's tooltip the whole subscription.
 */

import m from "mithril";
import type { LimitWindow } from "../models/providers";
import { resetText, ringFraction, widgetRows, windowText } from "../models/widget";
import type { LimitRow, SpendRow, WidgetModel } from "../models/widget";
import type { WidgetDetail } from "../models/widgetLook";
import { TRAY_NAME_CLASS, percentText, pillView, spendSectionView, tooltip, windowColour } from "./widgetParts";
import type { WindowRole } from "./widgetParts";

const BAR_HEIGHT: Record<WindowRole, string> = { short: "h-(--w-bar-short)", long: "h-(--w-bar-long)" };

/** One window's bar over its faint track, on a line tall enough to point at. */
function bar(window: LimitWindow, role: WindowRole): m.Vnode {
  return m(
    "span",
    {
      "data-widget-bar-line": role,
      class: "flex h-[1.1em] min-w-0 flex-[1_1_auto] items-center",
      ...tooltip(windowText(window)),
    },
    m(
      "span",
      { class: `w-full overflow-hidden rounded-full bg-(--widget-track) ${BAR_HEIGHT[role]}` },
      m("span", {
        "data-widget-bar": role,
        "data-widget-bar-level": window.level,
        class: "block h-full rounded-full",
        style: { width: `${ringFraction(window) * 100}%`, background: windowColour(window, role) },
      }),
    ),
  );
}

function figure(window: LimitWindow, role: WindowRole): m.Vnode {
  return m(
    "span",
    {
      "data-widget-percent": window.isResetSinceReading ? "reset" : window.level,
      class:
        "w-[2.8em] shrink-0 text-right tabular-nums " +
        (role === "short" ? "font-semibold text-(--widget-text)" : "text-[0.82em] text-(--widget-muted)"),
      ...tooltip(windowText(window)),
    },
    percentText(window),
  );
}

/** An expanded window's line: its bar, its reset time with more, and its figure. */
function windowLine(window: LimitWindow, role: WindowRole, detail: WidgetDetail): m.Vnode {
  return m("div", { class: "flex items-center gap-2" }, [
    bar(window, role),
    detail === "more"
      ? m(
          "span",
          {
            "data-widget-reset": role,
            class:
              "w-[5.8em] shrink-0 truncate whitespace-nowrap text-right text-[0.82em] tabular-nums text-(--widget-faint)",
          },
          resetText(window),
        )
      : null,
    figure(window, role),
  ]);
}

function limitRowView(row: LimitRow, detail: WidgetDetail): m.Vnode {
  const secondary = detail === "less" ? null : row.secondary;
  return m("div", { key: row.key, "data-widget-row": row.key, class: "flex items-center gap-2" }, [
    m("span", { class: "w-[4em] shrink-0 truncate text-(--widget-text)", ...tooltip(row.tooltip) }, row.name),
    m("div", { class: "flex min-w-0 flex-1 flex-col" }, [
      windowLine(row.primary, "short", detail),
      secondary === null ? null : windowLine(secondary, "long", detail),
    ]),
  ]);
}

function collapsedRowView(row: LimitRow, detail: WidgetDetail): m.Vnode {
  const secondary = detail === "less" ? null : row.secondary;
  return m("div", { key: row.key, "data-widget-row": row.key, class: "flex items-center gap-1.5" }, [
    m(
      "span",
      {
        class: "w-[5em] shrink-0 truncate text-[0.75em] font-semibold tracking-wider text-(--widget-muted) uppercase",
        ...tooltip(row.tooltip),
      },
      row.name,
    ),
    m("div", { class: "flex w-(--w-bar) flex-col" }, [
      bar(row.primary, "short"),
      secondary === null ? null : bar(secondary, "long"),
    ]),
    detail === "more" && secondary !== null
      ? m("div", { class: "flex flex-col" }, [figure(row.primary, "short"), figure(secondary, "long")])
      : figure(row.primary, "short"),
  ]);
}

export function barsExpanded(model: WidgetModel, detail: WidgetDetail): m.Children {
  const rows = widgetRows(model.cards ?? []);
  const limits = rows.filter((row): row is LimitRow => row.kind === "limit");
  const spends = rows.filter((row): row is SpendRow => row.kind === "spend");
  const sections: m.Vnode[] =
    limits.length === 0
      ? []
      : [
          m(
            "div",
            { key: "limits", class: "flex flex-col gap-1.5" },
            limits.map((row) => limitRowView(row, detail)),
          ),
        ];
  const spend = detail === "less" ? null : spendSectionView(spends);
  if (spend !== null) sections.push(spend);
  return m("div", { class: "flex flex-col gap-3" }, sections);
}

export function barsCollapsed(model: WidgetModel, detail: WidgetDetail): m.Children {
  const cards = model.cards ?? [];
  const limits = widgetRows(cards).filter((row): row is LimitRow => row.kind === "limit");
  if (limits.length > 0) {
    return m(
      "div",
      { class: "flex flex-col gap-1" },
      limits.map((row) => collapsedRowView(row, detail)),
    );
  }
  return pillView(cards);
}

export function barsTray(model: WidgetModel, detail: WidgetDetail): m.Children {
  const cards = model.cards ?? [];
  const limits = widgetRows(cards).filter((row): row is LimitRow => row.kind === "limit");
  if (limits.length === 0) return pillView(cards);
  return limits.map((row) => {
    const secondary = detail === "less" ? null : row.secondary;
    return m("div", { key: row.key, "data-widget-row": row.key, class: "flex items-center gap-1" }, [
      m("div", { class: "flex w-max min-w-(--w-bar) flex-col" }, [
        m("span", { class: TRAY_NAME_CLASS, ...tooltip(row.tooltip) }, row.name),
        bar(row.primary, "short"),
        secondary === null ? null : bar(secondary, "long"),
      ]),
      detail === "more" && secondary !== null
        ? m("div", { class: "flex flex-col" }, [figure(row.primary, "short"), figure(secondary, "long")])
        : figure(row.primary, "short"),
    ]);
  });
}
