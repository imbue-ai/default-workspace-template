/**
 * The usage chart: one bar per minute, hour or day of the window, stacked by model or by source, with a legend and
 * a tooltip per bar. Bars are plain SVG; the axis labels are HTML beside it so they stay legible at any width.
 */

import m from "mithril";

import type { TotalsRow } from "../models/api";
import { modelLabel } from "../models/breakdown";
import type { ChartMetric } from "../models/dashboard";
import { formatCount, formatDollars } from "../models/format";
import { OTHER_SERIES, type SeriesColors, seriesOf } from "../models/series";
import { BILLED_MODES } from "../models/summary";
import { totalTokens } from "../models/summary";
import { type WindowId, barLabel, binRows } from "../models/windows";

const VIEW_WIDTH = 720;
const VIEW_HEIGHT = 180;
const SEGMENT_GAP = 2;
const BAR_RADIUS = 4;

function valueOf(metric: ChartMetric, percentPerDollar: number): (row: TotalsRow) => number {
  if (metric === "tokens") return (row) => totalTokens(row.tokens);
  if (metric === "usage") {
    return (row) =>
      row.group.billing_mode === "SUBSCRIPTION" && row.cost_usd !== null ? Number(row.cost_usd) * percentPerDollar : 0;
  }
  return (row) =>
    (BILLED_MODES as readonly string[]).includes(row.group.billing_mode ?? "") && row.cost_usd !== null
      ? Number(row.cost_usd)
      : 0;
}

export function formatMetric(metric: ChartMetric, value: number): string {
  if (metric === "usage") return `${value.toFixed(value < 10 ? 1 : 0)}%`;
  return metric === "spend" ? formatDollars(value) : formatCount(value);
}

function seriesColor(slot: number | null): string {
  return slot === null ? "var(--series-other)" : `var(--series-${slot})`;
}

export interface ChartAttrs {
  windowId: WindowId;
  now: Date;
  rows: readonly TotalsRow[];
  metric: ChartMetric;
  colors: SeriesColors;
  /** The series a row belongs to: its model, unless the chart stacks by something else. */
  seriesLabel?: (row: TotalsRow) => string;
  /** What the series are, for the chart's accessible name: "model", "source". */
  seriesNoun?: string;
  /** For the usage metric: the share of a limit a dollar of list price uses. */
  percentPerDollar?: number;
  /** Moments to mark with a dashed line, such as a limit window starting or resetting. */
  markers?: readonly ChartMarker[];
  /** What the dashed lines mean, said once in the legend rather than on each line. */
  markerLegend?: string;
}

export interface ChartMarker {
  at: Date;
  label: string;
}

/** Where ``at`` falls across the bars, as a fraction of the chart's width; null outside them. */
export function markerPosition(starts: readonly Date[], now: Date, at: Date): number | null {
  const index = starts.findIndex((start, position) => {
    const end = starts[position + 1] ?? now;
    return at >= start && at < end;
  });
  if (index === -1) return null;
  const start = starts[index].getTime();
  const end = (starts[index + 1] ?? now).getTime();
  return (index + (end > start ? (at.getTime() - start) / (end - start) : 0)) / starts.length;
}

export function UsageChart(): m.Component<ChartAttrs> {
  let hovered: number | null = null;
  return {
    view({ attrs }) {
      const { windowId, now, rows, metric, colors, percentPerDollar, markers, markerLegend } = attrs;
      const labelOf = attrs.seriesLabel ?? ((row: TotalsRow) => modelLabel(row.group.model ?? null));
      const value = valueOf(metric, percentPerDollar ?? 0);
      const totalsBySeries = new Map<string, number>();
      for (const row of rows) {
        const series = labelOf(row);
        totalsBySeries.set(series, (totalsBySeries.get(series) ?? 0) + value(row));
      }
      const ranked = [...totalsBySeries.entries()]
        .filter(([, total]) => total > 0)
        .sort((a, b) => b[1] - a[1])
        .map(([series]) => series);
      const { shown, slotOf } = colors.assign(ranked);
      const order = [...shown, ...(ranked.some((series) => !shown.includes(series)) ? [OTHER_SERIES] : [])];
      const { starts, values } = binRows(windowId, now, rows, (row) => seriesOf(labelOf(row), shown), value);
      const barTotals = values.map((bar) => [...bar.values()].reduce((sum, amount) => sum + amount, 0));
      const peak = Math.max(0, ...barTotals);
      const peakIndex = barTotals.indexOf(peak);
      const placedMarkers = (markers ?? []).flatMap((marker) => {
        const position = markerPosition(starts, now, marker.at);
        return position === null ? [] : [{ marker, position }];
      });
      if (peak === 0) {
        return m(
          "div",
          { class: "chart-empty type-helper py-10 text-center text-faint" },
          "Nothing to chart in this window.",
        );
      }
      const slotWidth = VIEW_WIDTH / starts.length;
      const barWidth = Math.max(2, slotWidth - SEGMENT_GAP * 2);
      const scale = (amount: number): number => (amount / peak) * (VIEW_HEIGHT - 4);
      const hoveredBar = hovered !== null && hovered < starts.length ? hovered : null;

      return m("div", { class: "usage-chart flex flex-col gap-2" }, [
        // The room above the bars is for the label over the tallest one.
        m("div", { class: "relative pt-5" }, [
          m(
            "div",
            {
              class:
                "peak-label pointer-events-none absolute top-0 -translate-x-1/2 type-tag tabular-nums text-secondary",
              style: { left: `${((peakIndex + 0.5) / starts.length) * 100}%` },
            },
            formatMetric(metric, peak),
          ),
          placedMarkers.map(({ marker, position }) =>
            m(
              "div",
              {
                key: `${marker.label}:${marker.at.getTime()}`,
                class: "chart-marker absolute top-5 bottom-0 w-2 -translate-x-1/2",
                style: { left: `${position * 100}%` },
                title: `${marker.label}, ${marker.at.toLocaleString()}`,
              },
              m("div", { class: "mx-auto h-full w-0 border-l border-dashed border-(--color-faint)" }),
            ),
          ),
          m(
            "svg",
            {
              viewBox: `0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`,
              preserveAspectRatio: "none",
              class: "block h-44 w-full",
              role: "img",
              "aria-label": `${{ spend: "Spend", tokens: "Tokens", usage: "Estimated limit usage" }[metric]} over the window, by ${attrs.seriesNoun ?? "model"}`,
              onmouseleave: () => (hovered = null),
            },
            [
              m("line", {
                x1: 0,
                x2: VIEW_WIDTH,
                y1: VIEW_HEIGHT - 0.5,
                y2: VIEW_HEIGHT - 0.5,
                class: "stroke-(--color-subtle)",
              }),
              starts.map((_, index) => {
                let top = VIEW_HEIGHT;
                const segments = order
                  .map((series) => ({ series, amount: values[index].get(series) ?? 0 }))
                  .filter((segment) => segment.amount > 0);
                const x = index * slotWidth + (slotWidth - barWidth) / 2;
                return m(
                  "g",
                  { key: index, "data-bar": index, opacity: hoveredBar === null || hoveredBar === index ? 1 : 0.55 },
                  [
                    segments.map((segment, position) => {
                      const height = Math.max(1, scale(segment.amount) - (position > 0 ? SEGMENT_GAP : 0));
                      top -= height + (position > 0 ? SEGMENT_GAP : 0);
                      const isTop = position === segments.length - 1;
                      return m("rect", {
                        x,
                        y: top,
                        width: barWidth,
                        height,
                        rx: isTop ? Math.min(BAR_RADIUS, barWidth / 2) : 0,
                        fill: seriesColor(segment.series === OTHER_SERIES ? null : slotOf(segment.series)),
                      });
                    }),
                    m("rect", {
                      x: index * slotWidth,
                      y: 0,
                      width: slotWidth,
                      height: VIEW_HEIGHT,
                      fill: "transparent",
                      onmouseenter: () => (hovered = index),
                    }),
                  ],
                );
              }),
            ],
          ),
          hoveredBar === null
            ? null
            : m(
                "div",
                {
                  class:
                    "chart-tooltip pointer-events-none absolute top-2 z-10 min-w-40 rounded-md border border-default " +
                    "bg-surface px-2.5 py-2 type-helper text-primary shadow-sm",
                  style:
                    hoveredBar < starts.length / 2
                      ? { left: `${((hoveredBar + 1) / starts.length) * 100}%` }
                      : { right: `${(1 - hoveredBar / starts.length) * 100}%` },
                },
                [
                  m("div", { class: "mb-1 font-semibold" }, barLabel(windowId, starts[hoveredBar])),
                  order
                    .filter((series) => (values[hoveredBar].get(series) ?? 0) > 0)
                    .map((series) =>
                      m("div", { class: "flex items-center justify-between gap-3" }, [
                        m("span", { class: "flex items-center gap-1.5" }, [
                          m("span", {
                            class: "inline-block size-2 rounded-full",
                            style: { background: seriesColor(series === OTHER_SERIES ? null : slotOf(series)) },
                          }),
                          series,
                        ]),
                        m(
                          "span",
                          { class: "tabular-nums" },
                          formatMetric(metric, values[hoveredBar].get(series) ?? 0),
                        ),
                      ]),
                    ),
                  m("div", { class: "mt-1 flex justify-between border-t border-subtle pt-1 text-secondary" }, [
                    m("span", "Total"),
                    m("span", { class: "tabular-nums" }, formatMetric(metric, barTotals[hoveredBar])),
                  ]),
                ],
              ),
        ]),
        m("div", { class: "flex justify-between type-tag text-faint" }, [
          m("span", barLabel(windowId, starts[0])),
          m("span", barLabel(windowId, starts[starts.length - 1])),
        ]),
        m("div", { class: "chart-legend flex flex-wrap gap-x-4 gap-y-1 type-helper text-secondary" }, [
          ...order.map((series) =>
            m("span", { class: "flex items-center gap-1.5" }, [
              m("span", {
                class: "inline-block size-2.5 rounded-sm",
                style: { background: seriesColor(series === OTHER_SERIES ? null : slotOf(series)) },
              }),
              series,
            ]),
          ),
          placedMarkers.length === 0 || markerLegend === undefined
            ? null
            : m("span", { class: "marker-legend flex items-center gap-1.5" }, [
                m("span", { class: "inline-block h-3 w-0 border-l border-dashed border-(--color-faint)" }),
                markerLegend,
              ]),
        ]),
      ]);
    },
  };
}
