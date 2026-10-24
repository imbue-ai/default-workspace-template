/** Collector rows for the tests, with every field a test does not set left empty. */

import type { LatencyRow, TotalsRow } from "../models/api";

export function totalsRow(patch: Partial<TotalsRow>): TotalsRow {
  return {
    group: {},
    bucket_start: null,
    calls: 1,
    unknown_cost_calls: 0,
    cost_usd: null,
    tokens: { input: 0, output: 0, cache_read: 0, cache_write: 0 },
    ...patch,
  };
}

export function latencyRow(patch: Partial<LatencyRow>): LatencyRow {
  return { group: {}, timed_calls: 1, untimed_calls: 0, approximate_calls: 0, p50_ms: 1000, p95_ms: 2000, ...patch };
}
