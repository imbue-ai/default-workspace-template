import { describe, expect, it } from "vitest";

import { latencyRow, totalsRow } from "../testing/rows";
import {
  BREAKDOWNS,
  NO_SOURCE_NAMES,
  breakdownLines,
  filterOf,
  matchesFilter,
  sourceLabel,
  withFilterFields,
} from "./breakdown";

describe("BREAKDOWNS", () => {
  it("offers source first, then model and billing mode", () => {
    expect(BREAKDOWNS.map((spec) => spec.id)).toEqual(["source", "model", "billing_mode"]);
  });
});

describe("breakdownLines", () => {
  it("keeps spend and a subscription's list price apart within one model", () => {
    const tokens = { input: 10, output: 0, cache_read: 0, cache_write: 0 };
    const [line] = breakdownLines(
      "model",
      [
        totalsRow({ group: { model: "anthropic/opus", billing_mode: "IMBUE_KEY" }, calls: 2, cost_usd: "1", tokens }),
        totalsRow({
          group: { model: "anthropic/opus", billing_mode: "SUBSCRIPTION" },
          calls: 3,
          cost_usd: "4",
          tokens,
        }),
      ],
      [latencyRow({ group: { model: "anthropic/opus" }, p50_ms: 1500 })],
      NO_SOURCE_NAMES,
    );

    expect(line.label).toBe("opus");
    expect([line.calls, line.tokens, line.spend, line.billedCalls, line.listPrice]).toEqual([5, 20, 1, 2, 4]);
    expect(line.latency?.p50_ms).toBe(1500);
    expect(line.group).toEqual({ model: "anthropic/opus" });
  });

  it("orders lines by tokens, largest first", () => {
    const lines = breakdownLines(
      "billing_mode",
      [
        totalsRow({
          group: { billing_mode: "API_KEY" },
          tokens: { input: 1, output: 0, cache_read: 0, cache_write: 0 },
        }),
        totalsRow({
          group: { billing_mode: "IMBUE_KEY" },
          tokens: { input: 9, output: 0, cache_read: 0, cache_write: 0 },
        }),
      ],
      [],
      NO_SOURCE_NAMES,
    );

    expect(lines.map((line) => line.label)).toEqual(["Imbue key", "API key"]);
  });
});

describe("filters", () => {
  it("match a source by both its kind and id, with a missing id as empty", () => {
    const filter = filterOf("source", { origin_kind: "SYSTEM", origin_id: null });

    expect(matchesFilter({ origin_kind: "SYSTEM", origin_id: null, model: "x" }, filter)).toBe(true);
    expect(matchesFilter({ origin_kind: "APP", origin_id: null }, filter)).toBe(false);
    expect(matchesFilter({ origin_kind: "SYSTEM" }, null)).toBe(true);
  });

  it("add the filter's fields to a query's grouping once", () => {
    expect(withFilterFields(["model", "billing_mode"], filterOf("model", { model: "m" }))).toEqual([
      "model",
      "billing_mode",
    ]);
    expect(withFilterFields(["status"], filterOf("source", { origin_kind: "APP", origin_id: "files" }))).toEqual([
      "status",
      "origin_kind",
      "origin_id",
    ]);
  });
});

describe("sourceLabel", () => {
  it("names an app by its display name and shortens a long id", () => {
    const names = {
      apps: { files: "Files" },
      chats: { "agent-1": "Seattle weather" },
      routines: { "us-news": "US news" },
    };
    expect(sourceLabel("APP", "files", names)).toBe("App · Files");
    expect(sourceLabel("CHAT", "agent-1", names)).toBe("Chat · Seattle weather");
    expect(sourceLabel("ROUTINE", "us-news", names)).toBe("Routine · US news");
    expect(sourceLabel("CHAT", "c_8f2a1b9c3d4e", names)).toBe("Chat · c_8f2a1b9c…");
    expect(sourceLabel("SYSTEM", null, names)).toBe("System");
  });
});
