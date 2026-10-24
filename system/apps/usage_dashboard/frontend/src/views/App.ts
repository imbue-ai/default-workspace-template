/**
 * The Usage page: the window picker, the tiles, the chart, the breakdown and the latest calls.
 */

import m from "mithril";

import { bannerClass } from "@imbue/workspace-ui/src/components/banner";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { Dropdown } from "@imbue/workspace-ui/src/components/dropdown";
import { SegmentedControl } from "@imbue/workspace-ui/src/components/segmentedControl";

import { type LatencyRow, type StoredCall, type TotalsRow, type TurnRow, turnKey } from "../models/api";
import {
  WIDGET_DETAILS,
  WIDGET_DETAIL_LABELS,
  WIDGET_SIZES,
  WIDGET_SIZE_LABELS,
  WIDGET_PLACES,
  WIDGET_PLACE_LABELS,
  WIDGET_TOOLBARS,
  WIDGET_TOOLBAR_LABELS,
  WIDGET_TRAYS,
  WIDGET_TRAY_LABELS,
  WIDGET_VARIANTS,
  WIDGET_VARIANT_LABELS,
} from "../models/widgetLook";
import type { WidgetLook, WidgetLookPicker, WidgetPlace } from "../models/widgetLook";
import {
  BREAKDOWNS,
  type BreakdownId,
  billingModeLabel,
  breakdownLines,
  callGroup,
  filterKey,
  filterOf,
  groupLabel,
  modelLabel,
  type SourceNames,
  sourceLabel,
} from "../models/breakdown";
import type {
  ChartMetric,
  ChartSeries,
  DashboardData,
  DashboardModel,
  SectionFigures,
  UsageFigures,
} from "../models/dashboard";
import {
  formatCalls,
  formatCallTime,
  formatClock,
  formatCount,
  formatDollars,
  formatDuration,
  formatPercent,
  formatTurns,
} from "../models/format";
import {
  ROWS_PER_PAGE_CHOICES,
  type RowsPerPage,
  pageCount,
  pageNumbers,
  pageRange,
  typedPage,
} from "../models/callPages";
import { SeriesColors } from "../models/series";
import { BILLED_MODES, totalTokens } from "../models/summary";
import { type Route, pathOf } from "../models/routing";
import type { SectionKind } from "../models/sections";
import { type UsageScale, chosenScale, formatShare, limitShares, lineKey } from "../models/usageEstimate";
import { WINDOWS, type WindowId } from "../models/windows";
import { cardTitle } from "./cardTitle";
import { type ChartAttrs, type ChartMarker, UsageChart } from "./chart";
import { providerCard, providerSection } from "./providerCards";

const CARD_CLASS = "rounded-lg border border-default bg-surface p-4";

function tile(label: string, value: string, details: m.Children, attrs: m.Attributes = {}): m.Vnode {
  return m("div", { class: `${CARD_CLASS} flex min-w-0 flex-col gap-1`, ...attrs }, [
    cardTitle(label, "span"),
    m("span", { class: "type-heading-lg tabular-nums text-primary" }, value),
    m("div", { class: "type-helper text-secondary" }, details),
  ]);
}

function spendTile(data: UsageFigures): m.Vnode {
  const { summary } = data;
  const prefix = summary.isSpendEstimated ? "~" : "";
  const modeLines = BILLED_MODES.filter((mode) => summary.spendByMode[mode] !== undefined).map((mode) =>
    m("div", `${billingModeLabel(mode)} ${formatDollars(summary.spendByMode[mode] ?? 0)}`),
  );
  return tile(
    "Spend",
    `${prefix}${formatDollars(summary.spend)}`,
    [
      modeLines.length === 0 ? m("div", "Nothing billed") : modeLines,
      summary.isSpendEstimated ? m("div", { class: "text-faint" }, "~ includes costs estimated from tokens") : null,
      summary.unknownCostCalls > 0
        ? m(
            "div",
            { class: "text-warning", "data-unknown-cost": summary.unknownCostCalls },
            `+${formatCalls(summary.unknownCostCalls)} with unknown cost`,
          )
        : null,
    ],
    { "data-tile": "spend" },
  );
}

function tokensTile(data: UsageFigures): m.Vnode {
  const { tokens } = data.summary;
  return tile(
    "Tokens",
    formatCount(totalTokens(tokens)),
    [
      m("div", `${formatCount(tokens.input)} in · ${formatCount(tokens.output)} out`),
      m("div", `${formatCount(tokens.cache_read)} cache read · ${formatCount(tokens.cache_write)} cache write`),
    ],
    { "data-tile": "tokens" },
  );
}

function callsTile(data: UsageFigures): m.Vnode {
  const { calls, failed } = data.failed;
  return tile(
    "Calls",
    formatCount(calls),
    [
      m("div", `in ${formatTurns(data.turns)}`),
      failed === 0
        ? m("div", "None failed")
        : m("div", { class: "text-danger" }, `${formatPercent(failed, calls)} failed or rate-limited`),
    ],
    { "data-tile": "calls" },
  );
}

function latencyText(row: LatencyRow | null | undefined, which: "p50_ms" | "p95_ms"): string {
  const value = row?.[which];
  return value === null || value === undefined ? "–" : formatDuration(value);
}

function latencyTile(data: UsageFigures): m.Vnode {
  const row = data.latency;
  return tile(
    "Latency (p50)",
    latencyText(row, "p50_ms"),
    [
      m("div", `p95 ${latencyText(row, "p95_ms")}`),
      row !== null && row.untimed_calls > 0
        ? m("div", { class: "text-faint" }, `${formatCalls(row.untimed_calls)} without timing`)
        : null,
      row !== null && row.approximate_calls > 0
        ? m(
            "div",
            { class: "text-faint" },
            `${formatPercent(row.approximate_calls, row.timed_calls)} approximate (from session logs)`,
          )
        : null,
    ],
    { "data-tile": "latency" },
  );
}

/** A column for one limit: its scale and the breakdown rows within its most recent window. */
interface LimitColumn {
  scale: UsageScale;
  rows: readonly TotalsRow[];
}

/**
 * The usage columns the breakdown and the latest calls share on a page: a subscription's limit estimates, one
 * column per limit, or (with none) a money column, spend for the breakdown and cost for a call.
 */
type UsageColumns = readonly LimitColumn[];

function limitHeader(limit: LimitColumn, of: string): m.Vnode {
  return m(
    "th",
    {
      key: limit.scale.windowKey,
      class: "py-1.5 pr-3 text-right font-normal",
      title: `Estimated share of the ${limitName(limit.scale)} limit, ${of}`,
    },
    limit.scale.label.replace(" limit", ""),
  );
}

function breakdownSection(
  model: DashboardModel,
  breakdown: BreakdownId,
  section: SectionFigures,
  columns: UsageColumns,
): m.Vnode {
  const lines = breakdownLines(breakdown, section.breakdownRows, section.breakdownLatency, model.sourceNames);
  const limitColumns = columns.map((limit) => ({ ...limit, shares: limitShares(breakdown, limit.rows, limit.scale) }));
  const showsMoney = columns.length === 0;
  const activeKey = model.filter === null ? null : filterKey(model.filter);
  return m("div", { class: "flex flex-col gap-3" }, [
    m("div", { class: "flex flex-wrap items-center justify-between gap-2" }, [
      cardTitle("Breakdown"),
      m(SegmentedControl<BreakdownId>(), {
        label: "Break usage down by",
        segments: BREAKDOWNS.map((spec) => ({ value: spec.id, label: spec.label })),
        value: model.breakdown,
        onSelect: (id) => model.selectBreakdown(id),
      }),
    ]),
    lines.length === 0
      ? m("div", { class: "type-helper text-faint" }, "No calls in this window.")
      : m("div", { class: "overflow-x-auto" }, [
          m("table", { class: "breakdown-table w-full border-collapse type-body" }, [
            m("thead", [
              m("tr", { class: "border-b border-default text-left type-tag uppercase tracking-wide text-faint" }, [
                m("th", { class: "py-1.5 pr-3 font-normal" }, BREAKDOWNS.find((spec) => spec.id === breakdown)?.label),
                m("th", { class: "py-1.5 pr-3 text-right font-normal" }, "Calls"),
                m("th", { class: "py-1.5 pr-3 text-right font-normal" }, "Tokens"),
                showsMoney ? m("th", { class: "py-1.5 pr-3 text-right font-normal" }, "Spend") : null,
                limitColumns.map((limit) => limitHeader(limit, "within its most recent window")),
                m("th", { class: "py-1.5 text-right font-normal" }, "p50"),
              ]),
            ]),
            m(
              "tbody",
              lines.map((line) => {
                const filter = filterOf(breakdown, line.group);
                const isActive = activeKey === filterKey(filter);
                return m(
                  "tr",
                  {
                    key: filterKey(filter),
                    class: `breakdown-row cursor-pointer border-b border-subtle hover:bg-fill-hover ${isActive ? "bg-fill-active" : ""}`,
                    "data-breakdown-row": line.label,
                    "aria-selected": isActive ? "true" : "false",
                    title: isActive ? "Show everything again" : `Show only ${line.label}`,
                    onclick: () => model.toggleFilter(filter),
                  },
                  [
                    m("td", { class: "max-w-64 truncate py-2 pr-3 text-primary" }, line.label),
                    m("td", { class: "py-2 pr-3 text-right tabular-nums text-secondary" }, formatCount(line.calls)),
                    m("td", { class: "py-2 pr-3 text-right tabular-nums text-secondary" }, formatCount(line.tokens)),
                    showsMoney
                      ? m("td", { class: "py-2 pr-3 text-right tabular-nums text-secondary" }, [
                          line.billedCalls === 0 ? "–" : formatDollars(line.spend),
                        ])
                      : null,
                    limitColumns.map((limit) => {
                      const share = limit.shares.get(lineKey(line.group));
                      return m(
                        "td",
                        {
                          key: limit.scale.windowKey,
                          class: "breakdown-usage py-2 pr-3 text-right tabular-nums text-secondary",
                          "data-limit": limit.scale.windowKey,
                          title: `Estimated share of the ${limitName(limit.scale)} limit in its most recent window`,
                        },
                        share === undefined ? "–" : formatShare(share),
                      );
                    }),
                    m(
                      "td",
                      { class: "py-2 text-right tabular-nums text-secondary" },
                      latencyText(line.latency, "p50_ms"),
                    ),
                  ],
                );
              }),
            ),
          ]),
        ]),
    m(
      "p",
      { class: "type-helper text-faint" },
      showsMoney
        ? "Click a row to show only its usage. Spend counts only billed calls; subscription calls show none."
        : "Click a row to show only its usage. Each limit column is the row's estimated share of that limit within its most recent window.",
    ),
  ]);
}

function callCost(call: StoredCall): string {
  const { cost_usd: cost, billing_mode: mode, cost_provenance: provenance } = call.event;
  // A subscription call is covered by its plan, so it has no cost of its own to show.
  if (mode === "SUBSCRIPTION") return "Subscription";
  if (cost === null || cost === undefined) return "unknown";
  const amount = formatDollars(Number(cost));
  return provenance === "ESTIMATED" ? `~${amount}` : amount;
}

function callTokens(call: StoredCall): number {
  const tokens = call.event.tokens;
  return (tokens.input ?? 0) + (tokens.output ?? 0) + (tokens.cache_read ?? 0) + (tokens.cache_write ?? 0);
}

/** Where the scale's limit reset: when its window started, and when the window ended once that has happened. */
function resetMarkers(scale: UsageScale, now: Date): ChartMarker[] {
  const label = `${capitalized(limitName(scale))} reset`;
  return [{ at: scale.windowStart, label }, ...(scale.resetsAt <= now ? [{ at: scale.resetsAt, label }] : [])];
}

function capitalized(text: string): string {
  return text.length === 0 ? text : text[0].toUpperCase() + text.slice(1);
}

/** "5-hour", "weekly": the limit a usage scale is against. */
function limitName(scale: UsageScale): string {
  return scale.label.replace(" limit", "").toLowerCase();
}

/** A subscription call's estimated share of a limit, from its list price; null for any other call. */
function callShare(call: StoredCall, scale: UsageScale): number | null {
  const { billing_mode: mode, cost_usd: cost } = call.event;
  if (mode !== "SUBSCRIPTION" || cost === null || cost === undefined) return null;
  return Number(cost) * scale.percentPerDollar;
}

/** What one row of the latest turns shows, for a turn or one of its calls. */
interface ActivityCells {
  endedAt: Date;
  source: string;
  model: string;
  modelTitle?: string;
  calls: number | null;
  tokens: number;
  cost: string;
  /** The row's estimated share of each limit column, in the columns' order; null where it has none. */
  shares: (number | null)[];
  durationMs: number;
  status: { text: string; isOk: boolean };
}

function activityCells(cells: ActivityCells, now: Date, columns: UsageColumns, lead: m.Children): m.Children {
  const cell = "py-1.5 pr-3 text-right tabular-nums text-secondary";
  return [
    m("td", { class: "whitespace-nowrap py-1.5 pr-3 tabular-nums text-secondary" }, [
      lead,
      formatCallTime(cells.endedAt, now),
    ]),
    m("td", { class: "max-w-48 truncate py-1.5 pr-3 text-primary" }, cells.source),
    m("td", { class: "max-w-48 truncate py-1.5 pr-3 text-secondary", title: cells.modelTitle }, cells.model),
    m("td", { class: cell }, cells.calls === null ? "" : formatCount(cells.calls)),
    m("td", { class: cell }, formatCount(cells.tokens)),
    columns.length === 0 ? m("td", { class: cell }, cells.cost) : null,
    columns.map((limit, index) => {
      const share = cells.shares[index] ?? null;
      return m(
        "td",
        { key: limit.scale.windowKey, class: `call-usage ${cell}`, "data-limit": limit.scale.windowKey },
        share === null ? "–" : formatShare(share),
      );
    }),
    m("td", { class: cell }, cells.durationMs > 0 ? formatDuration(cells.durationMs) : "–"),
    m("td", { class: `py-1.5 ${cells.status.isOk ? "text-faint" : "text-danger"}` }, cells.status.text),
  ];
}

/**
 * The same row as two lines, for a screen too narrow for the table: what it was and what it used on the first
 * (its source, and its share of each limit or its cost), the rest muted on the second, its status only when a
 * call failed. A turn's call leads with its time instead: its source is the turn's, already on the line above.
 */
function activityLines(
  cells: ActivityCells,
  now: Date,
  columns: UsageColumns,
  lead: m.Children,
  heading: "source" | "time",
): m.Children {
  const usage =
    columns.length === 0
      ? [m("span", { class: "activity-usage" }, cells.cost)]
      : columns.map((limit, index) => {
          const share = cells.shares[index] ?? null;
          return m(
            "span",
            { class: "activity-usage", "data-limit": limit.scale.windowKey },
            `${share === null ? "–" : formatShare(share)} ${limitName(limit.scale)}`,
          );
        });
  const time = formatCallTime(cells.endedAt, now);
  const details = [
    ...(heading === "source" ? [time] : []),
    cells.model,
    ...(cells.calls === null ? [] : [`${formatCount(cells.calls)} ${cells.calls === 1 ? "call" : "calls"}`]),
    `${formatCount(cells.tokens)} tokens`,
    ...(cells.durationMs > 0 ? [formatDuration(cells.durationMs)] : []),
  ];
  return [
    m("div", { class: "flex items-baseline gap-2" }, [
      lead,
      m(
        "span",
        { class: "min-w-0 flex-1 truncate tabular-nums text-primary" },
        heading === "source" ? cells.source : time,
      ),
      m(
        "span",
        { class: "flex shrink-0 gap-2 tabular-nums text-secondary" },
        usage.flatMap((part, index) => (index === 0 ? [part] : [m("span", { class: "text-faint" }, "·"), part])),
      ),
    ]),
    // Under the first line's text: past the lead's w-6 and the gap-2 after it.
    m("div", { class: "flex flex-wrap gap-x-2 pl-8 type-helper tabular-nums text-faint", title: cells.modelTitle }, [
      details.join(" · "),
      cells.status.isOk ? null : m("span", { class: "text-danger" }, cells.status.text),
    ]),
  ];
}

function turnCells(turn: TurnRow, model: DashboardModel, columns: UsageColumns): ActivityCells {
  const subscription = turn.subscription_cost_usd === null ? null : Number(turn.subscription_cost_usd);
  const [first, ...rest] = turn.models;
  return {
    endedAt: new Date(turn.ended_at),
    source: sourceLabel(turn.origin_kind, turn.origin_id, model.sourceNames),
    model: first === undefined ? "Unknown model" : `${modelLabel(first)}${rest.length > 0 ? ` +${rest.length}` : ""}`,
    modelTitle: turn.models.map(modelLabel).join(", "),
    calls: turn.calls,
    tokens: totalTokens(turn.tokens),
    cost:
      turn.billed_cost_usd !== null
        ? `${turn.is_billed_cost_estimated ? "~" : ""}${formatDollars(Number(turn.billed_cost_usd))}`
        : turn.unknown_cost_calls > 0
          ? "unknown"
          : "Subscription",
    shares: columns.map((limit) => (subscription === null ? null : subscription * limit.scale.percentPerDollar)),
    durationMs: Date.parse(turn.ended_at) - Date.parse(turn.started_at),
    status:
      turn.failed_calls === 0
        ? { text: "ok", isOk: true }
        : { text: `${formatCount(turn.failed_calls)} failed`, isOk: false },
  };
}

function callCells(call: StoredCall, model: DashboardModel, columns: UsageColumns): ActivityCells {
  const event = call.event;
  const group = callGroup(event);
  return {
    endedAt: new Date(event.ended_at),
    source: sourceLabel(group.origin_kind, group.origin_id, model.sourceNames),
    model: modelLabel(event.model),
    calls: null,
    tokens: callTokens(call),
    cost: callCost(call),
    shares: columns.map((limit) => callShare(call, limit.scale)),
    durationMs: Date.parse(event.ended_at) - Date.parse(event.started_at),
    status: { text: event.status.toLowerCase().replace("_", " "), isOk: event.status === "OK" },
  };
}

/**
 * The latest turns: one row per turn (a user message and every call it led to, or a call no writer gave a turn),
 * newest first, each opening to its calls, and each call to its raw event.
 */
function turnsSection(model: DashboardModel, now: Date, columns: UsageColumns): m.Vnode {
  const showsMoney = columns.length === 0;
  const columnCount = 7 + (showsMoney ? 1 : columns.length);
  const callRows = (turn: TurnRow): m.Vnode[] => {
    const calls = model.expandedTurnCalls;
    if (calls === null) {
      return [
        m("tr", { key: `${turnKey(turn)}:loading`, class: "border-b border-subtle bg-surface-secondary" }, [
          m("td", { colspan: columnCount, class: "py-2 pl-6 type-helper text-faint" }, "Loading calls…"),
        ]),
      ];
    }
    return calls.flatMap((call) => {
      const event = call.event;
      const isOpen = model.expandedCallId === event.event_id;
      const rows = [
        m(
          "tr",
          {
            key: event.event_id,
            class: "call-row cursor-pointer border-b border-subtle bg-surface-secondary hover:bg-fill-hover",
            "data-call": event.event_id,
            title: isOpen ? "Hide the raw event" : "Show the raw event",
            onclick: () => model.toggleCall(event.event_id),
          },
          activityCells(callCells(call, model, columns), now, columns, m("span", { class: "inline-block w-6" })),
        ),
      ];
      if (isOpen) {
        rows.push(
          m("tr", { key: `${event.event_id}:raw`, class: "border-b border-subtle bg-surface-secondary" }, [
            m("td", { colspan: columnCount, class: "py-2 pl-6" }, [
              m(
                "pre",
                {
                  class:
                    "call-raw max-h-80 overflow-auto rounded-md bg-surface p-3 font-mono type-helper text-secondary",
                },
                JSON.stringify(event, null, 2),
              ),
            ]),
          ]),
        );
      }
      return rows;
    });
  };
  // Below md the same turns and calls as stacked two-line items, since the table needs about twice the width.
  const callItems = (turn: TurnRow): m.Vnode[] => {
    const calls = model.expandedTurnCalls;
    if (calls === null) {
      return [
        m("div", { key: `${turnKey(turn)}:loading`, class: "py-2 pl-6 type-helper text-faint" }, "Loading calls…"),
      ];
    }
    return calls.flatMap((call) => {
      const event = call.event;
      const isOpen = model.expandedCallId === event.event_id;
      return [
        m(
          "div",
          {
            key: event.event_id,
            class:
              "call-item cursor-pointer border-b border-subtle bg-surface-secondary py-2 pl-6 hover:bg-fill-hover",
            "data-call": event.event_id,
            title: isOpen ? "Hide the raw event" : "Show the raw event",
            onclick: () => model.toggleCall(event.event_id),
          },
          activityLines(
            callCells(call, model, columns),
            now,
            columns,
            m("span", { class: "inline-block w-6 shrink-0" }),
            "time",
          ),
        ),
        ...(isOpen
          ? [
              m(
                "div",
                { key: `${event.event_id}:raw`, class: "border-b border-subtle bg-surface-secondary py-2 pl-6" },
                [
                  m(
                    "pre",
                    {
                      class:
                        "call-raw max-h-80 overflow-auto rounded-md bg-surface p-3 font-mono type-helper text-secondary",
                    },
                    JSON.stringify(event, null, 2),
                  ),
                ],
              ),
            ]
          : []),
      ];
    });
  };
  const turnItems = model.turns.flatMap((turn) => {
    const key = turnKey(turn);
    const isOpen = model.expandedTurnKey === key;
    return [
      m(
        "div",
        {
          key,
          class: "turn-item cursor-pointer border-b border-subtle py-2 hover:bg-fill-hover",
          "data-turn": key,
          "aria-expanded": isOpen ? "true" : "false",
          title: isOpen ? "Hide its calls" : "Show its calls",
          onclick: () => model.toggleTurn(turn),
        },
        activityLines(
          turnCells(turn, model, columns),
          now,
          columns,
          m("span", { class: "inline-block w-6 shrink-0 text-faint", "aria-hidden": "true" }, isOpen ? "▾" : "▸"),
          "source",
        ),
      ),
      ...(isOpen ? callItems(turn) : []),
    ];
  });
  return m("section", { class: `${CARD_CLASS} flex flex-col gap-3` }, [
    m("div", { class: "flex items-center justify-between gap-2" }, [
      cardTitle("Latest turns"),
      m("span", { class: "type-helper text-faint" }, model.filter === null ? "All usage" : "Filtered"),
    ]),
    model.turns.length === 0
      ? m("div", { class: "type-helper text-faint" }, "No turns yet.")
      : m("div", { class: "turns-list flex flex-col type-body md:hidden" }, turnItems),
    model.turns.length === 0
      ? null
      : m("div", { class: "hidden overflow-x-auto md:block" }, [
          m("table", { class: "turns-table w-full border-collapse type-body" }, [
            m("thead", [
              m("tr", { class: "border-b border-default text-left type-tag uppercase tracking-wide text-faint" }, [
                m("th", { class: "py-1.5 pr-3 pl-6 font-normal" }, "Time"),
                m("th", { class: "py-1.5 pr-3 font-normal" }, "Source"),
                m("th", { class: "py-1.5 pr-3 font-normal" }, "Model"),
                m("th", { class: "py-1.5 pr-3 text-right font-normal" }, "Calls"),
                m("th", { class: "py-1.5 pr-3 text-right font-normal" }, "Tokens"),
                showsMoney ? m("th", { class: "py-1.5 pr-3 text-right font-normal" }, "Cost") : null,
                columns.map((limit) => limitHeader(limit, "from the list price of its calls")),
                m("th", { class: "py-1.5 pr-3 text-right font-normal" }, "Duration"),
                m("th", { class: "py-1.5 font-normal" }, "Status"),
              ]),
            ]),
            m(
              "tbody",
              model.turns.flatMap((turn) => {
                const key = turnKey(turn);
                const isOpen = model.expandedTurnKey === key;
                return [
                  m(
                    "tr",
                    {
                      key,
                      class: "turn-row cursor-pointer border-b border-subtle hover:bg-fill-hover",
                      "data-turn": key,
                      "aria-expanded": isOpen ? "true" : "false",
                      title: isOpen ? "Hide its calls" : "Show its calls",
                      onclick: () => model.toggleTurn(turn),
                    },
                    activityCells(
                      turnCells(turn, model, columns),
                      now,
                      columns,
                      m("span", { class: "inline-block w-6 text-faint", "aria-hidden": "true" }, isOpen ? "▾" : "▸"),
                    ),
                  ),
                  ...(isOpen ? callRows(turn) : []),
                ];
              }),
            ),
          ]),
        ]),
    callPager(model),
  ]);
}

/**
 * The latest turns' pager, laid out as data tables usually are: the calls shown on the left; on the right the
 * page numbers, how many rows to a page, and a box to jump to a page. Nothing while every call fits on the
 * smallest page.
 */
function callPager(model: DashboardModel): m.Vnode | null {
  const perPage = model.turnsPerPage;
  if (model.turnsTotal <= ROWS_PER_PAGE_CHOICES[0]) return null;
  const count = pageCount(model.turnsTotal, perPage);
  const current = model.turnsPage;
  const [first, last] = pageRange(current, perPage, model.turnsTotal);
  const pageButton = (page: number, label: string, attrs: Record<string, unknown>): m.Vnode =>
    m(
      Button,
      {
        variant: "ghost",
        extra: "min-w-[34px] px-2 tabular-nums",
        onclick: () => model.showTurnsPage(page),
        ...attrs,
      },
      label,
    );
  const step = (page: number, label: string, title: string): m.Vnode =>
    pageButton(page, label, { disabled: page < 1 || page > count, title, "aria-label": title });
  return m("nav", { class: "calls-pages flex flex-wrap items-center justify-between gap-3", "aria-label": "Pages" }, [
    m(
      "span",
      { class: "calls-range type-helper tabular-nums text-secondary" },
      `${first.toLocaleString()}–${last.toLocaleString()} of ${model.turnsTotal.toLocaleString()} turns`,
    ),
    m("div", { class: "flex flex-wrap items-center justify-end gap-x-4 gap-y-2" }, [
      count === 1
        ? null
        : m("div", { class: "flex items-center gap-1" }, [
            step(current - 1, "‹", "Previous page"),
            pageNumbers(current, count).map((item, index) =>
              item === "gap"
                ? m("span", { key: `gap${index}`, class: "min-w-[34px] text-center text-faint" }, "…")
                : pageButton(item, String(item), {
                    key: item,
                    selected: item === current,
                    "aria-current": item === current ? "page" : undefined,
                    "aria-label": `Page ${item}`,
                    "data-page": item,
                  }),
            ),
            step(current + 1, "›", "Next page"),
          ]),
      m("div", { class: "calls-per-page w-28" }, [
        m(Dropdown<string>, {
          options: ROWS_PER_PAGE_CHOICES.map((choice) => ({ value: String(choice), label: `${choice} / page` })),
          value: String(perPage),
          placeholder: `${perPage} / page`,
          "aria-label": "Rows per page",
          onSelect: (value: string) => model.selectTurnsPerPage(Number(value) as RowsPerPage),
        }),
      ]),
      count === 1
        ? null
        : m("label", { class: "flex items-center gap-2 whitespace-nowrap type-helper text-secondary" }, [
            "Go to",
            m("div", { class: "w-16" }, [
              m("input", {
                class: inputClass({ extra: "calls-go-to text-center tabular-nums" }),
                type: "text",
                inputmode: "numeric",
                "aria-label": "Go to page",
                onkeydown: (event: KeyboardEvent) => {
                  if (event.key !== "Enter") return;
                  const field = event.target as HTMLInputElement;
                  const page = typedPage(field.value, count);
                  field.value = "";
                  if (page !== null) model.showTurnsPage(page);
                },
              }),
            ]),
          ]),
    ]),
  ]);
}

function liveIndicator(model: DashboardModel): m.Vnode {
  return m(
    "span",
    {
      class: `live-indicator flex items-center gap-1.5 type-helper ${model.isLive ? "text-secondary" : "text-faint"}`,
      "data-live": model.isLive ? "true" : "false",
    },
    [
      m("span", {
        class: `inline-block size-2 rounded-full ${model.isLive ? "bg-(--color-success)" : "bg-(--color-faint)"}`,
      }),
      model.isLive ? "Live" : "Reconnecting",
    ],
  );
}

/** What the details below answer for: their window, and the filter a breakdown row set. */
function detailsControls(model: DashboardModel, names: SourceNames): m.Vnode {
  const filter = model.filter;
  return m("div", { class: "flex flex-wrap items-center justify-between gap-3" }, [
    filter === null
      ? m("span")
      : m(
          "button",
          {
            type: "button",
            class:
              "filter-chip flex items-center gap-1 rounded-full border border-default px-2.5 py-0.5 type-helper text-primary hover:bg-fill-hover",
            title: "Show everything again",
            onclick: () => model.toggleFilter(null),
          },
          [`Only ${groupLabel(filter.breakdown, filter.values, names)}`, m("span", { class: "text-faint" }, " ×")],
        ),
    m(SegmentedControl<WindowId>(), {
      label: "Time window",
      segments: WINDOWS.map((spec) => ({ value: spec.id, label: spec.label })),
      value: model.windowId,
      onSelect: (id) => model.selectWindow(id),
    }),
  ]);
}

function banners(model: DashboardModel): m.Children {
  return [
    model.isCollectorDown
      ? m(
          "div",
          { class: `${bannerClass("collector-down-banner", "warning")} rounded-md border` },
          model.data === null
            ? "The usage collector isn't responding. Retrying…"
            : `The usage collector isn't responding. Showing data from ${formatClock(model.data.loadedAt)}.`,
        )
      : null,
    model.isCatchingUp && !model.isCollectorDown
      ? m(
          "div",
          { class: `${bannerClass("catching-up-banner", "neutral")} rounded-md border` },
          "Still indexing past usage, so totals may be incomplete.",
        )
      : null,
    model.errorMessage === null
      ? null
      : m("div", { class: `${bannerClass("error-banner", "warning")} rounded-md border` }, model.errorMessage),
  ];
}

/** A set of calls' figures: their tiles, their usage over the window by source or by model, and their breakdown. */
/** Each chart keeps its own colours: a model's and a source's never compete for a slot. */
interface ChartColors {
  models: SeriesColors;
  sources: SeriesColors;
}

function figuresView(
  model: DashboardModel,
  data: DashboardData,
  figures: SectionFigures,
  kind: "all" | SectionKind,
  chartKey: string,
  now: Date,
  colors: ChartColors,
  columns: UsageColumns,
): m.Children {
  const isSubscription = kind === "subscription";
  const scales = isSubscription ? (model.usageScales[chartKey] ?? []) : [];
  const scale = chosenScale(scales, model.usageWindows[chartKey]);
  // A subscription's calls are not billed per call, so its chart is tokens or their estimated share of a limit.
  const choices: { value: ChartMetric; label: string }[] = isSubscription
    ? [
        ...(scale === undefined ? [] : [{ value: "usage" as const, label: "Usage" }]),
        { value: "tokens", label: "Tokens" },
      ]
    : [
        { value: "spend", label: "Spend" },
        { value: "tokens", label: "Tokens" },
      ];
  const picked = model.chartMetrics[chartKey];
  const metric: ChartMetric =
    picked !== undefined && choices.some((choice) => choice.value === picked)
      ? picked
      : isSubscription
        ? (choices[0]?.value ?? "tokens")
        : figures.summary.spend > 0
          ? "spend"
          : "tokens";
  const heading =
    metric === "usage" && scale !== undefined
      ? `Usage · estimated % of ${scale.label.replace(" limit", "").toLowerCase()} limit`
      : metric === "spend"
        ? "Spend"
        : "Tokens";
  const series: ChartSeries = model.chartSeries[chartKey] ?? "source";
  const chart = (attrs: Pick<ChartAttrs, "rows" | "colors" | "seriesLabel" | "seriesNoun">): m.Vnode =>
    m("div", { class: "usage-chart-panel flex min-w-0 flex-col", "data-chart": attrs.seriesNoun }, [
      m(UsageChart, {
        windowId: data.windowId,
        now,
        metric,
        percentPerDollar: scale?.percentPerDollar,
        markers: scale === undefined ? [] : resetMarkers(scale, now),
        markerLegend: scale === undefined ? undefined : `${capitalized(limitName(scale))} reset`,
        ...attrs,
      }),
    ]);
  const tiles =
    kind === "all"
      ? [spendTile(figures), tokensTile(figures), callsTile(figures), latencyTile(figures)]
      : [isSubscription ? null : spendTile(figures), tokensTile(figures), callsTile(figures), latencyTile(figures)];
  return [
    m("div", { class: "grid grid-cols-[repeat(auto-fit,minmax(140px,1fr))] gap-3" }, tiles),
    m("section", { class: `${CARD_CLASS} flex flex-col gap-3` }, [
      m("div", { class: "flex flex-wrap items-center justify-between gap-2" }, [
        cardTitle(heading),
        m("div", { class: "flex flex-wrap items-center gap-2" }, [
          metric === "usage" && scales.length > 1
            ? m(SegmentedControl<string>(), {
                label: "Limit",
                segments: scales.map((candidate) => ({
                  value: candidate.windowKey,
                  label: candidate.label.replace(" limit", ""),
                })),
                value: scale?.windowKey ?? "",
                onSelect: (windowKey) => model.selectUsageWindow(chartKey, windowKey),
              })
            : null,
          choices.length > 1
            ? m(SegmentedControl<ChartMetric>(), {
                label: "Chart",
                segments: choices,
                value: metric,
                onSelect: (choice) => model.selectChartMetric(chartKey, choice),
              })
            : null,
          m(SegmentedControl<ChartSeries>(), {
            label: "Split by",
            segments: [
              { value: "source", label: "By source" },
              { value: "model", label: "By model" },
            ],
            value: series,
            onSelect: (choice) => model.selectChartSeries(chartKey, choice),
          }),
        ]),
      ]),
      series === "source"
        ? chart({
            rows: figures.sourceChartRows,
            colors: colors.sources,
            seriesLabel: (row) =>
              sourceLabel(row.group.origin_kind ?? null, row.group.origin_id ?? null, model.sourceNames),
            seriesNoun: "source",
          })
        : chart({ rows: figures.chartRows, colors: colors.models, seriesNoun: "model" }),
      metric === "usage" && scale !== undefined
        ? m(
            "p",
            { class: "usage-estimate-note type-helper text-faint" },
            scale.isFromEarlierWindow
              ? `Estimated at the rate of the ${limitName(scale)} window that reset at ${formatClock(scale.resetsAt)}: each call's tokens are priced at API rates.`
              : "Estimated: each call's tokens are priced at API rates, at the rate this workspace's own calls have moved the limit.",
          )
        : null,
    ]),
    m("section", { class: CARD_CLASS }, breakdownSection(model, data.breakdown, figures, columns)),
  ];
}

function windowPicker(model: DashboardModel): m.Children {
  return m(SegmentedControl<WindowId>(), {
    label: "Time window",
    segments: WINDOWS.map((spec) => ({ value: spec.id, label: spec.label })),
    value: model.windowId,
    onSelect: (id) => model.selectWindow(id),
  });
}

/** One part of the widget's look, as a small menu. */
function lookMenu<T extends string>(
  choices: readonly T[],
  labels: Record<T, string>,
  value: T,
  label: string,
  onSelect: (value: T) => void,
): m.Vnode {
  return m("div", { class: "w-28" }, [
    m(Dropdown<T>, {
      options: choices.map((choice) => ({ value: choice, label: labels[choice] })),
      value,
      placeholder: labels[value],
      "aria-label": label,
      onSelect,
    }),
  ]);
}

/** The choices that apply where the widget is: its style and size on the desktop, or its design in the toolbar or
 *  the tray; how much it shows applies in every place. */
function placeChoices(picker: WidgetLookPicker, look: WidgetLook): m.Children {
  const detail = lookMenu(WIDGET_DETAILS, WIDGET_DETAIL_LABELS, look.detail, "How much the widget shows", (choice) => {
    void picker.select({ detail: choice });
  });
  switch (look.place) {
    case "desktop":
      return [
        lookMenu(WIDGET_VARIANTS, WIDGET_VARIANT_LABELS, look.variant, "Widget style", (variant) => {
          void picker.select({ variant });
        }),
        lookMenu(WIDGET_SIZES, WIDGET_SIZE_LABELS, look.size, "Widget size", (size) => void picker.select({ size })),
        detail,
      ];
    case "toolbar":
      return [
        lookMenu(WIDGET_TOOLBARS, WIDGET_TOOLBAR_LABELS, look.toolbar, "Widget design in the toolbar", (toolbar) => {
          void picker.select({ toolbar });
        }),
        detail,
      ];
    case "tray":
      return [
        lookMenu(WIDGET_TRAYS, WIDGET_TRAY_LABELS, look.tray, "Widget design in the tray", (tray) => {
          void picker.select({ tray });
        }),
        detail,
      ];
  }
}

/** The workspace's pick of where the widget goes -- the desktop, the taskbar's row (the toolbar) or its tray -- and,
 *  after it, the choices for that place alone, saved by the Usage app and shown by its widget at once. Every pick also
 *  puts this client's widget in the picked place, where it is seen. It sits at the foot of every page, on the left,
 *  out of the way of the figures: at the window's bottom edge when the page is shorter than the window. */
function widgetLookPicker(picker: WidgetLookPicker): m.Children {
  const look = picker.look;
  if (look === null) {
    return picker.errorMessage === null
      ? null
      : m("footer", { class: "widget-style mt-auto border-t border-default pt-3" }, [
          m("span", { class: "type-helper text-danger", role: "alert" }, picker.errorMessage),
        ]);
  }
  return m(
    "footer",
    { class: "widget-style mt-auto flex flex-wrap items-center gap-2 border-t border-default pt-3" },
    [
      picker.errorMessage === null
        ? null
        : m("span", { class: "type-helper text-danger", role: "alert" }, picker.errorMessage),
      m("span", { class: "type-helper text-secondary" }, "Widget"),
      m(SegmentedControl<WidgetPlace>(), {
        label: "Where the widget goes",
        segments: WIDGET_PLACES.map((place) => ({
          value: place,
          label: WIDGET_PLACE_LABELS[place],
          attrs: { "data-widget-place": place },
        })),
        value: look.place,
        onSelect: (place) => void picker.select({ place }),
      }),
      placeChoices(picker, look),
    ],
  );
}

/**
 * The page's header. A detail page passes the name of what it shows, which heads the page on a narrow screen,
 * where the list of providers is no longer a column beside it saying which one is open.
 */
function pageHeader(model: DashboardModel, back: boolean, title?: string): m.Vnode {
  return m("header", { class: "flex flex-wrap items-center gap-3" }, [
    back
      ? m(
          "a",
          {
            href: pathOf({ page: "home" }),
            class: "back-link type-body text-secondary no-underline hover:text-primary",
          },
          "‹ Back",
        )
      : null,
    title === undefined
      ? m("h1", { class: "type-heading text-primary" }, "Usage")
      : m("h1", { class: "page-title min-w-0 truncate type-heading text-primary" }, [
          m("span", { class: "hidden md:inline" }, "Usage"),
          m("span", { class: "md:hidden" }, title),
        ]),
    liveIndicator(model),
  ]);
}

/** The home page: one card per provider in use, then the Total row, which opens the first provider's details. */
function homePage(model: DashboardModel, now: Date): m.Children {
  const data = model.data;
  const firstProvider = model.providers?.[0];
  return [
    pageHeader(model, false),
    banners(model),
    m("div", { class: model.isCollectorDown ? "opacity-60" : "" }, providerSection(model.providers, now)),
    m(
      "section",
      { class: `total-row flex flex-col gap-2 ${model.isCollectorDown ? "opacity-60" : ""}`, "data-section": "total" },
      [
        m("div", { class: "flex flex-wrap items-center justify-between gap-3" }, [
          m("div", { class: "flex items-baseline gap-3" }, [
            m("h2", { class: "type-section text-primary" }, "Total"),
            // The details open on the first provider; the list there leads to the others.
            firstProvider === undefined
              ? null
              : m(
                  "a",
                  {
                    href: pathOf({ page: "item", key: firstProvider.sectionKey }),
                    class: "details-link type-helper text-secondary hover:text-primary",
                  },
                  "See details ›",
                ),
          ]),
          windowPicker(model),
        ]),
        data === null
          ? m(
              "div",
              { class: "type-helper py-6 text-center text-faint" },
              model.isCollectorDown ? "" : "Loading usage…",
            )
          : m("div", { class: "grid grid-cols-[repeat(auto-fit,minmax(140px,1fr))] gap-3" }, [
              spendTile(data.total),
              tokensTile(data.total),
              callsTile(data.total),
              latencyTile(data.total),
            ]),
      ],
    ),
  ];
}

interface NavItem {
  route: Route;
  key: string;
  title: string;
  note: string;
}

/** The detail page's list: the providers in the order of the home page's cards, then any other with calls. */
function navItems(model: DashboardModel): NavItem[] {
  const items: NavItem[] = [];
  for (const card of model.providers ?? []) {
    const note =
      card.kind === "subscription"
        ? card.windows
            .map(
              (window) =>
                `${window.label.replace(" limit", "")} ${window.usedPercent === null ? "–" : `${Math.round(window.usedPercent)}%`}`,
            )
            .join(" · ")
        : `${card.isEstimated ? "~" : ""}${formatDollars(card.spend)} this month`;
    items.push({ route: { page: "item", key: card.sectionKey }, key: card.sectionKey, title: card.title, note });
  }
  for (const section of model.data?.sections ?? []) {
    if (items.some((item) => item.key === section.spec.key)) continue;
    items.push({
      route: { page: "item", key: section.spec.key },
      key: section.spec.key,
      title: section.spec.title,
      note: "",
    });
  }
  return items;
}

/** A detail page: the list of providers, and the chosen one's limits, figures and latest calls. */
function detailPage(model: DashboardModel, route: Route, now: Date, colors: ChartColors): m.Children {
  const data = model.data;
  const selectedKey = route.page === "item" ? route.key : "all";
  const items = navItems(model);
  const selected = items.find((item) => item.key === selectedKey);
  const card = (model.providers ?? []).find((candidate) => candidate.sectionKey === selectedKey);
  const section = data?.sections.find((candidate) => candidate.spec.key === selectedKey);
  const kind: "all" | SectionKind =
    route.page === "item" ? (selectedKey.startsWith("subscription:") ? "subscription" : "spend") : "all";
  const figures = route.page === "all" ? data?.total : section;
  // A subscription's page measures both tables against its limits; any other page in money.
  const columns: UsageColumns =
    kind === "subscription"
      ? (model.usageScales[selectedKey] ?? []).flatMap((scale) => {
          const rows = section?.limitWindowRows?.[scale.windowKey];
          return rows === undefined ? [] : [{ scale, rows }];
        })
      : [];
  return [
    pageHeader(model, true, selected?.title ?? (route.page === "all" ? "All usage" : "Unknown provider")),
    banners(model),
    m("div", { class: "detail-layout grid gap-4 md:grid-cols-[15rem_minmax(0,1fr)]" }, [
      m(
        "nav",
        {
          // On a narrow screen the header names the open item, so a list with nothing else to open is left out.
          class: `detail-nav ${items.length > 1 ? "flex" : "hidden md:flex"} gap-1 overflow-x-auto md:flex-col md:overflow-visible`,
          "aria-label": "Providers",
        },
        items.map((item) => {
          const isSelected = item.key === selectedKey;
          return m(
            "a",
            {
              key: item.key,
              href: pathOf(item.route),
              class: `nav-item flex shrink-0 flex-col rounded-md px-3 py-2 no-underline ${isSelected ? "bg-fill-active" : "hover:bg-fill-hover"}`,
              "aria-current": isSelected ? "page" : undefined,
              "data-nav": item.key,
            },
            [
              m(
                "span",
                { class: `type-body ${isSelected ? "font-semibold text-primary" : "text-primary"}` },
                item.title,
              ),
              item.note === "" ? null : m("span", { class: "type-helper text-faint" }, item.note),
            ],
          );
        }),
      ),
      m("div", { class: `detail-content flex min-w-0 flex-col gap-4 ${model.isCollectorDown ? "opacity-60" : ""}` }, [
        // A card names its provider itself; without one the item needs a heading, which below md the h1 already is.
        card === undefined
          ? m(
              "h2",
              { class: "hidden type-heading text-primary md:block" },
              selected?.title ?? (route.page === "all" ? "All usage" : "Unknown provider"),
            )
          : null,
        card === undefined ? null : providerCard(card, now),
        detailsControls(model, model.sourceNames),
        data === null
          ? m(
              "div",
              { class: "type-helper py-10 text-center text-faint" },
              model.isCollectorDown ? "" : "Loading usage…",
            )
          : figures === undefined || figures.summary.calls === 0
            ? m(
                "div",
                { class: `${CARD_CLASS} empty-window type-body text-secondary` },
                data.windowId === "month"
                  ? "No calls this month."
                  : "No calls in this window. Try 7 days or this month.",
              )
            : figuresView(model, data, figures, kind, selectedKey, now, colors, columns),
        turnsSection(model, now, columns),
      ]),
    ]),
  ];
}

export interface AppAttrs {
  model: DashboardModel;
  lookPicker: WidgetLookPicker;
  route: Route;
  now: () => Date;
}

export function App(): m.Component<AppAttrs> {
  const colors: ChartColors = { models: new SeriesColors(), sources: new SeriesColors() };
  return {
    view({ attrs: { model, lookPicker, route, now } }) {
      const current = now();
      return m(
        "div",
        { class: "h-screen overflow-y-auto" },
        m("main", { class: "mx-auto flex min-h-full max-w-6xl flex-col gap-4 px-4 py-4 sm:px-6" }, [
          route.page === "home" ? homePage(model, current) : detailPage(model, route, current, colors),
          widgetLookPicker(lookPicker),
        ]),
      );
    },
  };
}
