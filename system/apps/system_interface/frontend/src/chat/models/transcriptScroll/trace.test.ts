import { describe, expect, it } from "vitest";
import { createScrollTrace, type ScrollTraceEntry } from "./trace";

function makeClock(): () => number {
  let tick = 0;
  return () => ++tick;
}

describe("createScrollTrace", () => {
  it("keeps entries in chronological order", () => {
    const trace = createScrollTrace({ capacityEntryCount: 10, now: makeClock(), echo: null });
    trace.record("transition", { to: "USER_CONTROLLED" });
    trace.record("compensation", { deltaPx: -3 });
    expect(trace.entries().map((entry) => entry.kind)).toEqual(["transition", "compensation"]);
  });

  it("wraps at capacity, keeping the newest entries oldest-first", () => {
    const trace = createScrollTrace({ capacityEntryCount: 3, now: makeClock(), echo: null });
    for (let i = 1; i <= 5; i++) {
      trace.record(`k${i}`, {});
    }
    expect(trace.entries().map((entry) => entry.kind)).toEqual(["k3", "k4", "k5"]);
  });

  it("echoes each entry as it is recorded", () => {
    const echoed: ScrollTraceEntry[] = [];
    const trace = createScrollTrace({ capacityEntryCount: 2, now: makeClock(), echo: (entry) => echoed.push(entry) });
    trace.record("anchor", { rowKey: "a" });
    expect(echoed).toHaveLength(1);
    expect(echoed[0].detail).toEqual({ rowKey: "a" });
  });

  it("clears fully and keeps working afterwards", () => {
    const trace = createScrollTrace({ capacityEntryCount: 2, now: makeClock(), echo: null });
    trace.record("a", {});
    trace.record("b", {});
    trace.record("c", {});
    trace.clear();
    expect(trace.entries()).toEqual([]);
    trace.record("d", {});
    expect(trace.entries().map((entry) => entry.kind)).toEqual(["d"]);
  });
});
