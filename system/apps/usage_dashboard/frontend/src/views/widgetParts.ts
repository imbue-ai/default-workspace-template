/**
 * What every version of the Usage widget draws alike (app-widgets plan section 7): the colour a window is shown in,
 * its figure, a tooltip, a section's heading, a pay-as-you-go row, the one figure of the month's spend a collapsed
 * widget falls back to with no subscription, and a mark per subscription for the taskbar's designs (else that figure).
 *
 * A tooltip is a ``data-widget-tooltip`` attribute, which the page's app contract hands to the shell to show as the
 * workspace's own tooltip outside the widget's small frame; nothing in the widget changes under the pointer. Sizes
 * are the ``--w-*`` lengths the widget's root takes from its size (``style.css``), and text is set in ``em`` so it
 * scales with them.
 */

import m from "mithril";
import { WIDGET_TOOLTIP_ATTRIBUTE } from "@imbue/workspace-ui/src/app_contract";
import type { LimitWindow, ProviderCard } from "../models/providers";
import { widgetPill, widgetRings, windowText } from "../models/widget";
import type { RingPair, SpendRow, WidgetModel } from "../models/widget";

/** Which of a subscription's windows a mark stands for: the shortest (5-hour) inside or above, the longest outside or
 *  below. */
export type WindowRole = "short" | "long";

// A comfortable window keeps its own hue; a high or critical one takes the level's colour.
const ROLE_COLOUR: Record<WindowRole, string> = { short: "var(--widget-short)", long: "var(--widget-long)" };

export function windowColour(window: LimitWindow, role: WindowRole): string {
  if (window.level === "critical") return "var(--widget-critical)";
  if (window.level === "high") return "var(--widget-high)";
  return ROLE_COLOUR[role];
}

export function percentText(window: LimitWindow): string {
  return window.isResetSinceReading || window.usedPercent === null ? "—" : `${Math.round(window.usedPercent)}%`;
}

/** The attribute an element's tooltip is carried in. */
export function tooltip(text: string): Record<string, string> {
  return { [WIDGET_TOOLTIP_ATTRIBUTE]: text };
}

/** A subscription's name in the taskbar's tray: small capitals, on one line, within the tray's height. */
export const TRAY_NAME_CLASS =
  "text-[0.7em] leading-none font-semibold tracking-wider whitespace-nowrap text-(--widget-muted) uppercase";

export function sectionHeading(name: string, attrs: Record<string, string> = {}): m.Vnode {
  return m(
    "span",
    { ...attrs, class: "text-[0.82em] leading-none font-semibold tracking-wider text-(--widget-heading) uppercase" },
    name,
  );
}

export function spendRowView(row: SpendRow): m.Vnode {
  return m(
    "div",
    { key: row.key, "data-widget-row": row.key, class: "flex items-center gap-1.5", ...tooltip(row.tooltip) },
    [
      m("span", { class: "inline-block size-1.5 shrink-0 rounded-full bg-(--widget-spend)" }),
      m("span", { class: "truncate text-(--widget-text)" }, row.name),
      m("span", { class: "ml-auto font-semibold tabular-nums text-(--widget-text)" }, row.spend),
    ],
  );
}

/** Pay-as-you-go spend under its heading, or nothing with none. */
export function spendSectionView(spends: readonly SpendRow[]): m.Vnode | null {
  if (spends.length === 0) return null;
  return m("div", { key: "spend", class: "flex flex-col gap-1" }, [
    sectionHeading("API this month"),
    m("div", { class: "flex flex-col gap-1" }, spends.map(spendRowView)),
  ]);
}

/** The collapsed widget with no subscription: one figure of this month's spend. */
export function pillView(cards: readonly ProviderCard[]): m.Vnode {
  const pill = widgetPill(cards);
  return m(
    "span",
    {
      class: "font-semibold whitespace-nowrap tabular-nums text-(--widget-text)",
      "data-widget-pill": pill?.level ?? "",
      ...(pill === null ? {} : tooltip(pill.tooltip)),
    },
    pill?.text ?? "",
  );
}

/** A window's figure, in its level's colour once it nears the cap. */
export function figure(window: LimitWindow, role: WindowRole, extra: string): m.Vnode {
  return m(
    "span",
    {
      "data-widget-percent": window.isResetSinceReading ? "reset" : window.level,
      class: `tabular-nums ${extra}`,
      style: window.level === "ok" ? {} : { color: windowColour(window, role) },
      ...tooltip(windowText(window)),
    },
    percentText(window),
  );
}

/** A mark per subscription, or with none the month's spend. */
export function perSubscription(model: WidgetModel, draw: (pair: RingPair) => m.Vnode): m.Children {
  const cards = model.cards ?? [];
  const pairs = widgetRings(cards);
  return pairs.length === 0 ? pillView(cards) : pairs.map(draw);
}
