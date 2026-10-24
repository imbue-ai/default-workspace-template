import { describe, expect, it } from "vitest";

import { type TurnRow, turnFilter, turnKey } from "./api";

function turn(turnId: string | null, eventId: string | null): TurnRow {
  return {
    turn_id: turnId,
    event_id: eventId,
    started_at: "2026-10-22T12:00:00Z",
    ended_at: "2026-10-22T12:00:05Z",
    calls: 1,
    failed_calls: 0,
    unknown_cost_calls: 0,
    tokens: { input: 0, output: 0, cache_read: 0, cache_write: 0 },
    billed_cost_usd: null,
    is_billed_cost_estimated: false,
    subscription_cost_usd: null,
    models: [],
    origin_kind: "CHAT",
    origin_id: null,
  };
}

describe("turns", () => {
  it("knows a turn by its turn id and asks for its calls by it", () => {
    expect(turnKey(turn("t-1", null))).toBe("t-1");
    expect(turnFilter(turn("t-1", null))).toEqual({ turn_id: ["t-1"] });
  });

  it("knows a call with no turn id by the call, and asks for that one call", () => {
    expect(turnKey(turn(null, "e-1"))).toBe("call:e-1");
    expect(turnFilter(turn(null, "e-1"))).toEqual({ event_id: ["e-1"] });
  });
});
