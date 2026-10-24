/**
 * What the page breaks usage down by, and the filter a click on a breakdown row sets.
 *
 * A filter is one value of one breakdown: a model, a source (an origin kind and id) or a billing mode. The
 * collector's totals take no filter, so the page asks for totals grouped by the filter's fields as well and keeps
 * the matching rows; its calls take the filter as query parameters.
 */

import { type Group, type LatencyRow, type ModelCall, type TotalsRow } from "./api";
import { totalTokens } from "./summary";

export type BreakdownId = "model" | "source" | "billing_mode";

export interface BreakdownSpec {
  id: BreakdownId;
  label: string;
  fields: readonly string[];
}

export const BREAKDOWNS: readonly BreakdownSpec[] = [
  { id: "source", label: "Source", fields: ["origin_kind", "origin_id"] },
  { id: "model", label: "Model", fields: ["model"] },
  { id: "billing_mode", label: "Billing mode", fields: ["billing_mode"] },
];

export function breakdownSpec(id: BreakdownId): BreakdownSpec {
  const spec = BREAKDOWNS.find((candidate) => candidate.id === id);
  if (spec === undefined) throw new Error(`Unknown breakdown ${id}`);
  return spec;
}

export interface Filter {
  breakdown: BreakdownId;
  /** The value of each of the breakdown's fields; "" for "not known". */
  values: Record<string, string>;
}

export function filterOf(breakdown: BreakdownId, group: Group): Filter {
  const values: Record<string, string> = {};
  for (const field of breakdownSpec(breakdown).fields) values[field] = group[field] ?? "";
  return { breakdown, values };
}

export function filterKey(filter: Filter): string {
  return `${filter.breakdown}:${JSON.stringify(filter.values)}`;
}

/** The fields a query groups by: ``fields``, plus the filter's so its rows can be picked out. */
export function withFilterFields(fields: readonly string[], filter: Filter | null): string[] {
  return [...new Set([...fields, ...(filter === null ? [] : Object.keys(filter.values))])];
}

export function matchesFilter(group: Group, filter: Filter | null): boolean {
  if (filter === null) return true;
  return Object.entries(filter.values).every(([field, value]) => (group[field] ?? "") === value);
}

/** A call's values for the breakdown fields, read off its event. */
export function callGroup(call: ModelCall): Group {
  return {
    model: call.model,
    provider: call.provider,
    billing_mode: call.billing_mode,
    origin_kind: call.attribution.origin_kind,
    origin_id: call.attribution.origin_id ?? null,
    agent_id: call.attribution.agent_id ?? null,
  };
}

const ORIGIN_KIND_LABELS: Record<string, string> = {
  APP: "App",
  CHAT: "Chat",
  ROUTINE: "Routine",
  SYSTEM: "System",
  UNKNOWN: "Unknown",
};

const BILLING_MODE_LABELS: Record<string, string> = {
  IMBUE_KEY: "Imbue key",
  API_KEY: "API key",
  SUBSCRIPTION: "Subscription",
  UNKNOWN: "Unknown",
};

export function billingModeLabel(mode: string | null): string {
  return BILLING_MODE_LABELS[mode ?? "UNKNOWN"] ?? mode ?? "Unknown";
}

/** A model id without its provider: "claude-opus-5-5" for "anthropic/claude-opus-5-5". */
export function modelLabel(model: string | null): string {
  if (model === null || model === "") return "Unknown model";
  const slash = model.indexOf("/");
  return slash === -1 ? model : model.slice(slash + 1);
}

/** The names a source is shown under: an app's display name, a chat's or a routine's title, each by its id. */
export interface SourceNames {
  apps: Record<string, string>;
  chats: Record<string, string>;
  routines: Record<string, string>;
}

export const NO_SOURCE_NAMES: SourceNames = { apps: {}, chats: {}, routines: {} };

/**
 * "App · Files", "Chat · Seattle weather", "Routine · US news": a source by the name its owner gives it, else by a
 * short id.
 */
export function sourceLabel(kind: string | null, id: string | null, names: SourceNames): string {
  const kindLabel = ORIGIN_KIND_LABELS[kind ?? "UNKNOWN"] ?? kind ?? "Unknown";
  if (id === null || id === "") return kindLabel;
  const named =
    kind === "APP"
      ? names.apps[id]
      : kind === "CHAT"
        ? names.chats[id]
        : kind === "ROUTINE"
          ? names.routines[id]
          : undefined;
  if (named !== undefined) return `${kindLabel} · ${named}`;
  if (kind === "APP") return `${kindLabel} · ${id}`;
  return `${kindLabel} · ${id.length > 10 ? `${id.slice(0, 10)}…` : id}`;
}

export function groupLabel(breakdown: BreakdownId, group: Group, names: SourceNames): string {
  switch (breakdown) {
    case "model":
      return modelLabel(group.model ?? null);
    case "source":
      return sourceLabel(group.origin_kind ?? null, group.origin_id ?? null, names);
    case "billing_mode":
      return billingModeLabel(group.billing_mode ?? null);
  }
}

export interface BreakdownLine {
  label: string;
  /** The breakdown fields' values, which a click filters to. */
  group: Group;
  calls: number;
  tokens: number;
  /** What the line's Imbue key and API key calls billed. */
  spend: number;
  billedCalls: number;
  /** The line's subscription usage at list price; null with none priced. */
  listPrice: number | null;
  latency: LatencyRow | undefined;
}

/**
 * The breakdown's lines, largest first by tokens, from totals grouped by the breakdown's fields and
 * ``billing_mode`` (so spend and a subscription's list price stay apart), each with its latency.
 */
export function breakdownLines(
  breakdown: BreakdownId,
  rows: readonly TotalsRow[],
  latencyRows: readonly LatencyRow[],
  names: SourceNames,
): BreakdownLine[] {
  const fields = breakdownSpec(breakdown).fields;
  const lines = new Map<string, BreakdownLine>();
  for (const row of rows) {
    const group: Group = {};
    for (const field of fields) group[field] = row.group[field] ?? null;
    const key = JSON.stringify(group);
    let line = lines.get(key);
    if (line === undefined) {
      line = {
        label: groupLabel(breakdown, group, names),
        group,
        calls: 0,
        tokens: 0,
        spend: 0,
        billedCalls: 0,
        listPrice: null,
        latency: latencyRows.find((candidate) =>
          fields.every((field) => (candidate.group[field] ?? null) === group[field]),
        ),
      };
      lines.set(key, line);
    }
    line.calls += row.calls;
    line.tokens += totalTokens(row.tokens);
    const cost = row.cost_usd === null ? null : Number(row.cost_usd);
    const mode = row.group.billing_mode;
    if (mode === "IMBUE_KEY" || mode === "API_KEY") {
      line.billedCalls += row.calls;
      line.spend += cost ?? 0;
    } else if (mode === "SUBSCRIPTION" && cost !== null) {
      line.listPrice = (line.listPrice ?? 0) + cost;
    }
  }
  return [...lines.values()].sort((a, b) => b.tokens - a.tokens || b.calls - a.calls);
}
