/**
 * The rings version of the Usage widget (app-widgets plan sections 7.4, 7.5 and 7.8). A subscription is a pair of
 * concentric rings, the weekly limit outside and the 5-hour limit inside, each over a faint track, with the 5-hour
 * figure in the middle. How much it shows follows the workspace's pick:
 *
 * - Collapsed: the ring pair with the subscription's name beneath; with less, the 5-hour ring alone; with more, the
 *   name beside the pair over the weekly figure and the 5-hour reset time.
 * - Expanded: a section per subscription headed by its name, the pair beside a row per window (a dot in its ring's
 *   colour, its name, its figure); with less, the 5-hour window alone and no spend; with more, each row's reset time
 *   and the reading's time in view. Then the pay-as-you-go spend.
 *
 * - In the taskbar's tray: per subscription a small ring pair beside its name in small capitals over the 5-hour
 *   figure (and the weekly one with more), all within the tray's height.
 *
 * The minimal style is the ring pairs alone, the 5-hour figure in each, in a row with nothing else in view: with
 * less, the 5-hour ring alone; with more, the weekly figure under each pair. Collapsed, the same a size smaller
 * (``style.css``).
 *
 * The rest is in tooltips: the pair's says both windows, a row's its window, a heading the whole subscription.
 */

import m from "mithril";
import type { LimitWindow } from "../models/providers";
import { resetText, ringFraction, widgetRings, widgetRows, windowName, windowText } from "../models/widget";
import type { LimitRow, RingPair, SpendRow, WidgetModel } from "../models/widget";
import type { WidgetDetail } from "../models/widgetLook";
import {
  TRAY_NAME_CLASS,
  figure,
  percentText,
  perSubscription,
  pillView,
  sectionHeading,
  spendSectionView,
  tooltip,
  windowColour,
} from "./widgetParts";
import type { WindowRole } from "./widgetParts";

// The rings' drawing, in the units of its own view box: two circles about one centre, a stroke's width apart.
const RING_BOX = 40;
const RING_CENTRE = RING_BOX / 2;
const OUTER_RADIUS = 18;
const INNER_RADIUS = 13.5;
const RING_STROKE = 3;

const RING_OF_ROLE: Record<WindowRole, "inner" | "outer"> = { short: "inner", long: "outer" };

/** One ring: a faint full track, and over it, from twelve o'clock clockwise, the arc of the share used. */
function ringView(window: LimitWindow, radius: number, role: WindowRole): m.Vnode[] {
  const fraction = ringFraction(window);
  const circle = { cx: RING_CENTRE, cy: RING_CENTRE, r: radius, fill: "none", "stroke-width": RING_STROKE };
  const track = m("circle", { ...circle, stroke: "var(--widget-track)" });
  if (fraction === 0) return [track];
  const arc = m("circle", {
    ...circle,
    "data-ring": RING_OF_ROLE[role],
    "data-ring-level": window.level,
    stroke: windowColour(window, role),
    pathLength: 100,
    "stroke-dasharray": `${fraction * 100} 100`,
    "stroke-linecap": "round",
    transform: `rotate(-90 ${RING_CENTRE} ${RING_CENTRE})`,
  });
  return [track, arc];
}

/** A ring pair with the 5-hour figure in its middle (unless ``hasFigure`` is false, as in the tray); ``outer`` null
 *  draws the 5-hour ring alone. */
export function ringsView(inner: LimitWindow, outer: LimitWindow | null, label: string, hasFigure = true): m.Vnode {
  return m(
    "span",
    {
      class: "relative inline-flex size-(--w-ring) shrink-0 items-center justify-center",
      ...tooltip(label),
    },
    [
      m(
        "svg",
        {
          viewBox: `0 0 ${RING_BOX} ${RING_BOX}`,
          class: "absolute inset-0 size-full",
          "aria-label": label,
          role: "img",
        },
        [...(outer === null ? [] : ringView(outer, OUTER_RADIUS, "long")), ...ringView(inner, INNER_RADIUS, "short")],
      ),
      hasFigure
        ? m(
            "span",
            {
              "data-ring-figure": "",
              class:
                "relative text-(length:--w-ring-figure) leading-none font-semibold tabular-nums text-(--widget-text)",
            },
            percentText(inner),
          )
        : null,
    ],
  );
}

const NAME_CLASS = "text-[0.75em] leading-none font-semibold tracking-wider text-(--widget-muted) uppercase";

function ringPairView(pair: RingPair, detail: WidgetDetail): m.Vnode {
  const outer = detail === "less" ? null : pair.outer;
  const name = m("span", { "data-ring-name": "", class: NAME_CLASS }, pair.name);
  if (detail !== "more") {
    return m("span", { key: pair.key, "data-widget-rings": pair.key, class: "flex flex-col items-center gap-1" }, [
      ringsView(pair.inner, outer, pair.tooltip),
      name,
    ]);
  }
  return m("span", { key: pair.key, "data-widget-rings": pair.key, class: "flex items-center gap-1.5" }, [
    ringsView(pair.inner, outer, pair.tooltip),
    m("span", { class: "flex flex-col gap-0.5 text-[0.82em] whitespace-nowrap tabular-nums text-(--widget-muted)" }, [
      name,
      pair.outer === null
        ? null
        : m("span", { "data-ring-more": "long", ...tooltip(windowText(pair.outer)) }, `wk ${percentText(pair.outer)}`),
      m("span", { "data-ring-more": "short", ...tooltip(windowText(pair.inner)) }, `5h ${resetText(pair.inner)}`),
    ]),
  ]);
}

function windowRowView(window: LimitWindow, role: WindowRole, detail: WidgetDetail): m.Vnode {
  return m(
    "div",
    { "data-widget-window": window.key, class: "flex items-center gap-1.5", ...tooltip(windowText(window)) },
    [
      m("span", {
        class: "inline-block size-1.5 shrink-0 rounded-full",
        style: { background: windowColour(window, role) },
      }),
      m("span", { class: "shrink-0 text-(--widget-text)" }, windowName(window)),
      detail === "more"
        ? m(
            "span",
            { "data-widget-reset": "", class: "ml-auto min-w-0 truncate tabular-nums text-(--widget-faint)" },
            resetText(window),
          )
        : m("span", { class: "ml-auto" }),
      m(
        "span",
        {
          "data-widget-percent": window.isResetSinceReading ? "reset" : window.level,
          class: "w-[2.8em] shrink-0 text-right font-semibold tabular-nums text-(--widget-text)",
        },
        percentText(window),
      ),
    ],
  );
}

function limitSectionView(row: LimitRow, detail: WidgetDetail): m.Vnode {
  const secondary = detail === "less" ? null : row.secondary;
  return m("div", { key: row.key, "data-widget-row": row.key, class: "flex flex-col gap-1.5" }, [
    m("div", { class: "flex items-baseline gap-2" }, [
      sectionHeading(row.name, tooltip(row.tooltip)),
      detail === "more" ? m("span", { class: "ml-auto text-[0.8em] text-(--widget-faint)" }, row.asOf) : null,
    ]),
    m("div", { class: "flex items-center gap-2.5" }, [
      ringsView(row.primary, secondary, row.tooltip),
      m("div", { class: "flex min-w-0 flex-1 flex-col gap-1" }, [
        windowRowView(row.primary, "short", detail),
        secondary === null ? null : windowRowView(secondary, "long", detail),
      ]),
    ]),
  ]);
}

export function ringsExpanded(model: WidgetModel, detail: WidgetDetail): m.Children {
  const rows = widgetRows(model.cards ?? []);
  const limits = rows.filter((row): row is LimitRow => row.kind === "limit");
  const spends = rows.filter((row): row is SpendRow => row.kind === "spend");
  const sections: m.Vnode[] = limits.map((row) => limitSectionView(row, detail));
  const spend = detail === "less" ? null : spendSectionView(spends);
  if (spend !== null) sections.push(spend);
  return m("div", { class: "flex flex-col gap-3" }, sections);
}

export function ringsCollapsed(model: WidgetModel, detail: WidgetDetail): m.Children {
  const cards = model.cards ?? [];
  const rings = widgetRings(cards);
  if (rings.length > 0) {
    return m(
      "span",
      { class: "flex items-start gap-3" },
      rings.map((pair) => ringPairView(pair, detail)),
    );
  }
  return pillView(cards);
}

/** A minimal ring pair: the rings and the 5-hour figure, and with more the weekly figure beneath. */
function minimalPairView(pair: RingPair, detail: WidgetDetail): m.Vnode {
  return m("span", { key: pair.key, "data-widget-rings": pair.key, class: "flex flex-col items-center gap-0.5" }, [
    ringsView(pair.inner, detail === "less" ? null : pair.outer, pair.tooltip),
    detail === "more" && pair.outer !== null
      ? m(
          "span",
          {
            "data-ring-more": "long",
            class: "text-[0.75em] leading-none tabular-nums text-(--widget-muted)",
            ...tooltip(windowText(pair.outer)),
          },
          `wk ${percentText(pair.outer)}`,
        )
      : null,
  ]);
}

/** The minimal style, expanded or collapsed alike: a row of ring pairs, or with no subscription the month's spend. */
export function ringsMinimal(model: WidgetModel, detail: WidgetDetail): m.Children {
  const cards = model.cards ?? [];
  const rings = widgetRings(cards);
  if (rings.length === 0) return pillView(cards);
  return m(
    "span",
    { class: "flex items-start gap-2" },
    rings.map((pair) => minimalPairView(pair, detail)),
  );
}

export function ringsTray(model: WidgetModel, detail: WidgetDetail): m.Children {
  return perSubscription(model, (pair) =>
    m("span", { key: pair.key, "data-widget-rings": pair.key, class: "flex items-center gap-1" }, [
      ringsView(pair.inner, detail === "less" ? null : pair.outer, pair.tooltip, false),
      m("span", { class: "flex flex-col gap-px whitespace-nowrap tabular-nums leading-none" }, [
        m("span", { "data-ring-name": "", class: TRAY_NAME_CLASS, ...tooltip(pair.tooltip) }, pair.name),
        m("span", { class: "flex items-baseline gap-1" }, [
          figure(pair.inner, "short", "font-semibold"),
          detail === "more" && pair.outer !== null
            ? figure(pair.outer, "long", "text-[0.82em] text-(--widget-muted)")
            : null,
        ]),
      ]),
    ]),
  );
}
