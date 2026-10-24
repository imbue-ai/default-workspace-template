/**
 * The tiles' numbers, from totals grouped by billing mode and cost provenance.
 *
 * Spend is only what is billed per call: Imbue key and API key calls. A subscription's calls are priced at list
 * price by the collector but not billed, so they are summed apart. A call whose cost is unknown, or whose billing
 * mode is, is counted rather than added as $0, so the page can say the total leaves it out.
 */

import { TOKEN_BUCKETS, type TokenBucket, type TotalsRow } from "./api";

export const BILLED_MODES = ["IMBUE_KEY", "API_KEY"] as const;
export type BilledMode = (typeof BILLED_MODES)[number];

export interface Summary {
  calls: number;
  /** Billed dollars per billed mode, for the modes that had calls. */
  spendByMode: Partial<Record<BilledMode, number>>;
  spend: number;
  /** Whether any billed dollar was estimated from tokens rather than reported. */
  isSpendEstimated: boolean;
  /** Calls left out of spend: no known cost, or no known billing mode. */
  unknownCostCalls: number;
  tokens: Record<TokenBucket, number>;
}

function emptyTokens(): Record<TokenBucket, number> {
  return { input: 0, output: 0, cache_read: 0, cache_write: 0 };
}

export function totalTokens(tokens: Record<TokenBucket, number>): number {
  return TOKEN_BUCKETS.reduce((sum, bucket) => sum + tokens[bucket], 0);
}

function isBilled(mode: string | null): mode is BilledMode {
  return mode === "IMBUE_KEY" || mode === "API_KEY";
}

/** The summary of rows grouped (at least) by ``billing_mode`` and ``cost_provenance``. */
export function summarize(rows: readonly TotalsRow[]): Summary {
  const summary: Summary = {
    calls: 0,
    spendByMode: {},
    spend: 0,
    isSpendEstimated: false,
    unknownCostCalls: 0,
    tokens: emptyTokens(),
  };
  for (const row of rows) {
    summary.calls += row.calls;
    for (const bucket of TOKEN_BUCKETS) summary.tokens[bucket] += row.tokens[bucket] ?? 0;
    const mode = row.group.billing_mode ?? null;
    const cost = row.cost_usd === null ? null : Number(row.cost_usd);
    if (isBilled(mode)) {
      summary.unknownCostCalls += row.unknown_cost_calls;
      summary.spendByMode[mode] = (summary.spendByMode[mode] ?? 0) + (cost ?? 0);
      summary.spend += cost ?? 0;
      if (cost !== null && cost > 0 && row.group.cost_provenance === "ESTIMATED") summary.isSpendEstimated = true;
    } else if (mode !== "SUBSCRIPTION") {
      summary.unknownCostCalls += row.calls;
    }
  }
  return summary;
}

const FAILED_STATUSES: ReadonlySet<string> = new Set(["ERROR", "RATE_LIMITED", "REFUSED"]);

/** The share of calls that failed (error, rate limit or refusal), from rows grouped by ``status``. */
export function failedShare(rows: readonly TotalsRow[]): { calls: number; failed: number } {
  let calls = 0;
  let failed = 0;
  for (const row of rows) {
    calls += row.calls;
    if (row.group.status !== null && row.group.status !== undefined && FAILED_STATUSES.has(row.group.status)) {
      failed += row.calls;
    }
  }
  return { calls, failed };
}
