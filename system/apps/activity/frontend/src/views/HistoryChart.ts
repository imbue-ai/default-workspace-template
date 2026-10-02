/**
 * Memory over time, and what was closed: a line of the typical reading in each period over a band from its lowest to
 * its highest (so a resting level and a spike both show), the line where closing starts, and a mark wherever the
 * memory guard closed something. Hovering finds the nearest period; the readings are also a table. Below it, the
 * closures of the last week in plain words.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import type { ClosedProcess, HistoryPeriod, HistoryRange, HistoryState, HistoryView } from "../models/history";
import {
  CHART_BOX,
  bandPoints,
  chartMaxKib,
  linePoints,
  nearestPeriod,
  periodLabel,
  timeTicks,
  unbrokenRuns,
  xFor,
  yFor,
} from "./chartGeometry";
import type { ChartScale } from "./chartGeometry";
import { formatKib } from "./format";
import { DETAILS_CLASS, SECTION_HEADING_CLASS, disclosure } from "./styles";

const RANGE_LABELS: Record<HistoryRange, string> = { HOUR: "Last hour", DAY: "Last 24 hours", WEEK: "Last 7 days" };

export interface HistoryChartAttrs {
  readonly state: HistoryState;
  readonly range: HistoryRange;
  readonly onRange: (range: HistoryRange) => void;
}

function closureTime(closure: ClosedProcess): string {
  return new Date(closure.at).toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function HistoryChart(): m.Component<HistoryChartAttrs> {
  // The hovered period by its start, so a refresh or a new range never shows a period that is no longer drawn.
  let hoveredStartSeconds: number | null = null;
  let isTableOpen = false;
  let isHowOpen = false;
  // The drawing is laid out at the width it is shown at, so its labels stay legible on a narrow window rather than
  // shrinking with a scaled viewBox.
  let widthPx = CHART_BOX.width;
  let resizeObserver: ResizeObserver | null = null;

  function observeWidth(element: Element): void {
    if (typeof ResizeObserver === "undefined") return;
    resizeObserver = new ResizeObserver((entries) => {
      const measured = Math.round(entries[0]?.contentRect.width ?? 0);
      if (measured > 0 && measured !== widthPx) {
        widthPx = measured;
        m.redraw();
      }
    });
    resizeObserver.observe(element);
  }

  function scaleOf(view: HistoryView): ChartScale {
    return {
      startSeconds: view.end_epoch_seconds - view.span_seconds,
      endSeconds: view.end_epoch_seconds,
      maxKib: chartMaxKib(view.periods, view.limit_kib),
    };
  }

  function recordingNote(view: HistoryView): m.Children {
    if (!view.is_recording && view.periods.length === 0) {
      return m(
        "p",
        { class: "m-0 type-helper text-secondary" },
        "Nothing recorded yet. System Monitor takes a reading every minute once it has started; the chart fills in from there.",
      );
    }
    if (!view.is_recording) {
      return m(
        "p",
        { class: "m-0 type-helper text-secondary" },
        "Recording has paused, so the most recent minutes are missing.",
      );
    }
    const start = view.end_epoch_seconds - view.span_seconds;
    if (view.first_sample_epoch_seconds !== null && view.first_sample_epoch_seconds > start) {
      return m(
        "p",
        { class: "m-0 type-helper text-secondary" },
        `Recording since ${periodLabel(view.range, view.first_sample_epoch_seconds)}.`,
      );
    }
    return null;
  }

  function chart(view: HistoryView): m.Vnode {
    const box = { ...CHART_BOX, width: widthPx };
    const scale = scaleOf(view);
    const runs = unbrokenRuns(view.periods, view.period_seconds);
    const plotBottom = box.height - box.bottom;
    const yTicks = [0, scale.maxKib / 2, scale.maxKib];
    const onPointer = (event: PointerEvent): void => {
      const svg = (event.currentTarget as SVGGraphicsElement).ownerSVGElement;
      if (svg === null) return;
      const rect = svg.getBoundingClientRect();
      const x = ((event.clientX - rect.left) / rect.width) * box.width;
      hoveredStartSeconds =
        nearestPeriod(box, scale, view.periods, view.period_seconds, x)?.start_epoch_seconds ?? null;
    };
    const hovered: HistoryPeriod | null =
      view.periods.find((period) => period.start_epoch_seconds === hoveredStartSeconds) ?? null;
    const hoveredX = hovered === null ? null : xFor(box, scale, hovered.start_epoch_seconds + view.period_seconds / 2);
    const hoveredClosures =
      hovered === null
        ? []
        : view.closures_in_range.filter((closure) => {
            const at = Date.parse(closure.at) / 1000;
            return at >= hovered.start_epoch_seconds && at < hovered.start_epoch_seconds + view.period_seconds;
          });
    const isTooltipLeftOfCrosshair = hoveredX !== null && hoveredX > box.width * 0.6;
    return m(
      "div",
      {
        class: "activity-history-chart relative",
        oncreate: ({ dom }: m.VnodeDOM) => observeWidth(dom),
        onremove: () => {
          resizeObserver?.disconnect();
          resizeObserver = null;
        },
      },
      [
        m(
          "svg",
          {
            viewBox: `0 0 ${box.width} ${box.height}`,
            class: "block h-auto w-full overflow-visible",
            role: "img",
            "aria-label": `Memory in use over the ${RANGE_LABELS[view.range].toLowerCase()}; the readings are in the table below`,
          },
          [
            ...yTicks.map((kib) =>
              m("g", [
                m("line", {
                  x1: box.left,
                  x2: box.width - box.right,
                  y1: yFor(box, scale, kib),
                  y2: yFor(box, scale, kib),
                  class: "stroke-subtle",
                  "stroke-width": 1,
                }),
                m(
                  "text",
                  {
                    x: box.left - 6,
                    y: yFor(box, scale, kib) + 4,
                    "text-anchor": "end",
                    class: "fill-secondary text-[11px] tabular-nums",
                  },
                  kib === 0 ? "0" : formatKib(kib),
                ),
              ]),
            ),
            ...timeTicks(view.range, scale.startSeconds, scale.endSeconds).map((tick, index, ticks) =>
              m(
                "text",
                {
                  x: xFor(box, scale, tick.at),
                  y: box.height - 8,
                  "text-anchor": index === 0 ? "start" : index === ticks.length - 1 ? "end" : "middle",
                  class: "fill-secondary text-[11px]",
                },
                tick.label,
              ),
            ),
            ...runs.map((run) =>
              m("polygon", { points: bandPoints(box, scale, run, view.period_seconds), class: "fill-accent/15" }),
            ),
            ...runs.map((run) =>
              run.length === 1
                ? m("circle", {
                    cx: xFor(box, scale, run[0].start_epoch_seconds + view.period_seconds / 2),
                    cy: yFor(box, scale, run[0].average_kib),
                    r: 3,
                    class: "fill-accent",
                  })
                : m("polyline", {
                    points: linePoints(box, scale, run, view.period_seconds),
                    class: "fill-none stroke-accent",
                    "stroke-width": 2,
                    "stroke-linejoin": "round",
                    "stroke-linecap": "round",
                  }),
            ),
            view.closes_at_kib === null
              ? null
              : m("g", [
                  m("line", {
                    x1: box.left,
                    x2: box.width - box.right,
                    y1: yFor(box, scale, view.closes_at_kib),
                    y2: yFor(box, scale, view.closes_at_kib),
                    class: "stroke-danger-hover",
                    "stroke-width": 1.5,
                    "stroke-dasharray": "5 4",
                  }),
                  m(
                    "text",
                    {
                      x: box.left + 4,
                      y: yFor(box, scale, view.closes_at_kib) - 5,
                      "text-anchor": "start",
                      class: "fill-danger-hover text-[11px]",
                    },
                    "Closing starts",
                  ),
                ]),
            ...view.closures_in_range.map((closure) => {
              const x = xFor(box, scale, Date.parse(closure.at) / 1000);
              return m("g", { class: "activity-closure-mark" }, [
                m("title", `${closureTime(closure)}: closed ${closure.what}`),
                m("line", {
                  x1: x,
                  x2: x,
                  y1: box.top,
                  y2: plotBottom,
                  class: "stroke-danger-hover/40",
                  "stroke-width": 1,
                }),
                m("polygon", {
                  points: `${x - 5},${box.top - 8} ${x + 5},${box.top - 8} ${x},${box.top}`,
                  class: "fill-danger-hover",
                }),
              ]);
            }),
            hoveredX === null
              ? null
              : m("line", {
                  x1: hoveredX,
                  x2: hoveredX,
                  y1: box.top,
                  y2: plotBottom,
                  class: "stroke-secondary",
                  "stroke-width": 1,
                }),
            m("rect", {
              x: box.left,
              y: box.top,
              width: box.width - box.left - box.right,
              height: plotBottom - box.top,
              class: "fill-transparent",
              onpointermove: onPointer,
              onpointerleave: () => (hoveredStartSeconds = null),
            }),
          ],
        ),
        hovered === null || hoveredX === null
          ? null
          : m(
              "div",
              {
                class:
                  "activity-history-tooltip pointer-events-none absolute top-6 flex flex-col gap-0.5 rounded-md border border-default bg-surface px-2 py-1 type-helper whitespace-nowrap text-primary shadow-raised " +
                  (isTooltipLeftOfCrosshair ? "-translate-x-full -ml-2" : "ml-2"),
                style: { left: `${hoveredX}px` },
              },
              [
                m("span", { class: "text-secondary" }, periodLabel(view.range, hovered.start_epoch_seconds)),
                m("span", `Typical ${formatKib(hovered.average_kib)}`),
                m("span", `Lowest ${formatKib(hovered.min_kib)} · highest ${formatKib(hovered.max_kib)}`),
                ...hoveredClosures.map((closure) =>
                  m("span", { class: "text-danger-hover" }, `Closed ${closure.what}`),
                ),
              ],
            ),
      ],
    );
  }

  function legend(): m.Vnode {
    return m("div", { class: "flex flex-wrap items-center gap-x-4 gap-y-1 type-helper text-secondary" }, [
      m("span", { class: "inline-flex items-center gap-1.5" }, [
        m("span", { class: "inline-block h-0.5 w-4 rounded-sm bg-accent" }),
        "Typical use",
      ]),
      m("span", { class: "inline-flex items-center gap-1.5" }, [
        m("span", { class: "inline-block h-2.5 w-4 rounded-sm bg-accent/15" }),
        "Lowest to highest in each period",
      ]),
      m("span", { class: "inline-flex items-center gap-1.5" }, [
        m("span", { class: "inline-block h-2.5 w-0.5 bg-danger-hover" }),
        "Something was closed",
      ]),
    ]);
  }

  function readingsTable(view: HistoryView): m.Vnode {
    return m(
      "div",
      { class: `${DETAILS_CLASS} max-h-64 overflow-y-auto` },
      view.periods.length === 0
        ? "No readings in this range."
        : m("table", { class: "w-full border-collapse tabular-nums" }, [
            m(
              "thead",
              m("tr", [
                m("th", { class: "pr-3 text-left font-medium text-secondary" }, "Period from"),
                m("th", { class: "pr-3 text-right font-medium text-secondary" }, "Typical"),
                m("th", { class: "pr-3 text-right font-medium text-secondary" }, "Lowest"),
                m("th", { class: "pr-3 text-right font-medium text-secondary" }, "Highest"),
                m("th", { class: "text-right font-medium text-secondary" }, "Readings"),
              ]),
            ),
            m(
              "tbody",
              [...view.periods]
                .reverse()
                .map((period) =>
                  m("tr", { key: period.start_epoch_seconds }, [
                    m("td", { class: "pr-3" }, periodLabel(view.range, period.start_epoch_seconds)),
                    m("td", { class: "pr-3 text-right" }, formatKib(period.average_kib)),
                    m("td", { class: "pr-3 text-right" }, formatKib(period.min_kib)),
                    m("td", { class: "pr-3 text-right" }, formatKib(period.max_kib)),
                    m("td", { class: "text-right" }, String(period.sample_count)),
                  ]),
                ),
            ),
          ]),
    );
  }

  function recentlyClosed(view: HistoryView): m.Vnode {
    return m("section", { class: "flex flex-col gap-2" }, [
      m("h3", { class: `m-0 border-b border-default pb-1.5 ${SECTION_HEADING_CLASS}` }, "Recently closed"),
      view.recent_closures.length === 0
        ? m(
            "p",
            { class: "m-0 type-helper text-secondary" },
            "Nothing has been closed to free memory in the last week." +
              (view.closer === "SYSTEM_LIMIT"
                ? " On this workspace the system itself closes things at the memory limit, and those closures aren't recorded."
                : ""),
          )
        : view.recent_closures.map((closure) =>
            m(
              "div",
              {
                key: `${closure.at}-${closure.pid}`,
                class: "flex flex-col gap-0.5 border-b border-subtle py-2 last:border-b-0",
              },
              [
                m("span", { class: "type-body text-primary" }, [
                  m("span", { class: "text-secondary" }, `${closureTime(closure)} · `),
                  `Closed ${closure.what}` +
                    (closure.freed_kib === null ? "" : `, freeing about ${formatKib(closure.freed_kib)}`),
                ]),
                m("span", { class: "type-helper text-secondary" }, closure.next_step),
              ],
            ),
          ),
    ]);
  }

  return {
    view: ({ attrs }) => {
      const { state } = attrs;
      const header = m("div", { class: "flex flex-wrap items-center justify-between gap-2" }, [
        m("h3", { class: `m-0 ${SECTION_HEADING_CLASS}` }, "Memory over time"),
        m(
          "div",
          { class: "flex gap-1", role: "group", "aria-label": "Time range" },
          (Object.keys(RANGE_LABELS) as HistoryRange[]).map((range) =>
            m(
              Button,
              {
                key: range,
                variant: "ghost",
                sm: true,
                quiet: true,
                selected: attrs.range === range,
                extra: "activity-range",
                "aria-pressed": String(attrs.range === range),
                onclick: () => attrs.onRange(range),
              },
              RANGE_LABELS[range],
            ),
          ),
        ),
      ]);
      if (state.kind === "loading") {
        return m("section", { class: "flex flex-col gap-3" }, [
          header,
          m("p", { class: "m-0 type-helper text-secondary" }, "Reading the history…"),
        ]);
      }
      if (state.kind === "failed") {
        return m("section", { class: "flex flex-col gap-3" }, [
          header,
          m("p", { class: "m-0 type-helper text-danger-hover" }, `Couldn't read the history. ${state.message}`),
        ]);
      }
      const view = state.view;
      return m("div", { class: "flex flex-col gap-6" }, [
        m("section", { class: "flex flex-col gap-3", "aria-label": "Memory over time" }, [
          header,
          recordingNote(view),
          chart(view),
          legend(),
          m("div", { class: "flex flex-wrap gap-x-2" }, [
            disclosure(isTableOpen, "Show the readings", "Hide the readings", () => (isTableOpen = !isTableOpen)),
            disclosure(isHowOpen, "How this is recorded", "Hide how this is recorded", () => (isHowOpen = !isHowOpen)),
          ]),
          isTableOpen ? readingsTable(view) : null,
          isHowOpen
            ? m("div", { class: DETAILS_CLASS }, [
                m("div", `readings   one a minute, by cron, appended to ${view.history_path}; a week is kept`),
                m(
                  "div",
                  "periods    each point is the typical reading of its period; the band runs from its lowest to highest",
                ),
                m("div", "missed     a spike shorter than a minute can fall between two readings"),
                m("div", `closures   from the memory guard's own record, ${view.ledger_path}`),
              ])
            : null,
        ]),
        recentlyClosed(view),
      ]);
    },
  };
}
