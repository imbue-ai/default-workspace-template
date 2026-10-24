import { describe, expect, it } from "vitest";

import { totalsRow } from "../testing/rows";
import { failedShare, summarize } from "./summary";

describe("summarize", () => {
  it("counts only Imbue key and API key calls as spend", () => {
    const summary = summarize([
      totalsRow({ group: { billing_mode: "IMBUE_KEY", cost_provenance: "REPORTED" }, calls: 2, cost_usd: "1.5" }),
      totalsRow({ group: { billing_mode: "API_KEY", cost_provenance: "REPORTED" }, calls: 1, cost_usd: "0.25" }),
      totalsRow({ group: { billing_mode: "SUBSCRIPTION", cost_provenance: "ESTIMATED" }, calls: 4, cost_usd: "9" }),
    ]);

    expect(summary.spend).toBe(1.75);
    expect(summary.spendByMode).toEqual({ IMBUE_KEY: 1.5, API_KEY: 0.25 });
    expect(summary.isSpendEstimated).toBe(false);
    expect(summary.calls).toBe(7);
  });

  it("marks spend estimated when a billed cost was priced from tokens", () => {
    const summary = summarize([
      totalsRow({ group: { billing_mode: "API_KEY", cost_provenance: "ESTIMATED" }, cost_usd: "0.1" }),
    ]);

    expect(summary.isSpendEstimated).toBe(true);
  });

  it("counts calls with no known cost or billing mode instead of adding them as $0", () => {
    const summary = summarize([
      totalsRow({ group: { billing_mode: "API_KEY", cost_provenance: null }, calls: 3, unknown_cost_calls: 3 }),
      totalsRow({ group: { billing_mode: "UNKNOWN", cost_provenance: "ESTIMATED" }, calls: 2, cost_usd: "5" }),
    ]);

    expect(summary.spend).toBe(0);
    expect(summary.unknownCostCalls).toBe(5);
  });
});

describe("failedShare", () => {
  it("counts errors, rate limits and refusals as failed", () => {
    const share = failedShare([
      totalsRow({ group: { status: "OK" }, calls: 6 }),
      totalsRow({ group: { status: "ERROR" }, calls: 1 }),
      totalsRow({ group: { status: "RATE_LIMITED" }, calls: 2 }),
      totalsRow({ group: { status: "CANCELLED" }, calls: 1 }),
    ]);

    expect(share).toEqual({ calls: 10, failed: 3 });
  });
});
