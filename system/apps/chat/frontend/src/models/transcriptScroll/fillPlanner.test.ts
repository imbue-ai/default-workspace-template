import { describe, expect, it } from "vitest";
import { planNextFill, type FillPlanInput } from "./fillPlanner";
import type { PhysicalExtent } from "./types";

function plan(overrides: Partial<FillPlanInput>): ReturnType<typeof planNextFill> {
  return planNextFill({
    physical: null,
    totalEvents: null,
    focus: { kind: "tail" },
    capEvents: 1000,
    chunkLimit: 100,
    initialTailLimit: 10,
    jumpWindowLimit: 50,
    ...overrides,
  });
}

function window(firstIndex: number, endIndex: number): PhysicalExtent {
  return { firstIndex, endIndex };
}

describe("planNextFill", () => {
  it("fetches the instant tail page before anything is known", () => {
    expect(plan({})).toEqual({ kind: "fetch-tail", limit: 10 });
  });

  it("is idle for an empty chat", () => {
    expect(plan({ totalEvents: 0, physical: window(0, 0) })).toEqual({ kind: "idle" });
  });

  it("lands a window around a restored anchor when nothing is loaded", () => {
    const action = plan({ totalEvents: 10_000, physical: window(0, 0), focus: { kind: "index", index: 5000 } });
    expect(action).toEqual({ kind: "fetch-at-offset", offset: 4975, limit: 50 });
  });

  it("grows older history in chunks while following the tail", () => {
    const action = plan({ totalEvents: 10_000, physical: window(9990, 10_000) });
    expect(action).toEqual({ kind: "fetch-before", limit: 100 });
  });

  it("caps a growth fetch by the remaining budget", () => {
    const action = plan({ totalEvents: 10_000, physical: window(9030, 10_000), capEvents: 1000 });
    expect(action).toEqual({ kind: "fetch-before", limit: 30 });
  });

  it("extends toward a nearby out-of-window focus", () => {
    const action = plan({ totalEvents: 10_000, physical: window(5000, 5100), focus: { kind: "index", index: 4950 } });
    expect(action).toEqual({ kind: "fetch-before", limit: 100 });
    const after = plan({ totalEvents: 10_000, physical: window(5000, 5100), focus: { kind: "index", index: 5150 } });
    expect(after).toEqual({ kind: "fetch-after", limit: 100 });
  });

  it("replaces the window in one read when the focus is far away (deep fling / jump)", () => {
    const action = plan({ totalEvents: 10_000, physical: window(5000, 5100), focus: { kind: "index", index: 200 } });
    expect(action).toEqual({ kind: "fetch-at-offset", offset: 175, limit: 50 });
  });

  it("clamps a window replace at the transcript edges", () => {
    const nearStart = plan({ totalEvents: 10_000, physical: window(0, 0), focus: { kind: "index", index: 3 } });
    expect(nearStart).toEqual({ kind: "fetch-at-offset", offset: 0, limit: 50 });
    const nearEnd = plan({ totalEvents: 10_000, physical: window(0, 0), focus: { kind: "index", index: 9998 } });
    expect(nearEnd).toEqual({ kind: "fetch-at-offset", offset: 9950, limit: 50 });
  });

  it("grows toward the larger deficit around an interior focus", () => {
    // Focus 5000 inside 4990..5200: desired coverage is 4500..5500, so the
    // before-deficit (490) beats the after-deficit (300).
    const action = plan({ totalEvents: 10_000, physical: window(4990, 5200), focus: { kind: "index", index: 5000 } });
    expect(action).toEqual({ kind: "fetch-before", limit: 100 });
  });

  it("is idle once coverage is complete", () => {
    const action = plan({ totalEvents: 100, physical: window(0, 100), focus: { kind: "index", index: 50 } });
    expect(action).toEqual({ kind: "idle" });
  });

  it("is idle at the cap when the window is roughly centered", () => {
    const action = plan({
      totalEvents: 100_000,
      physical: window(49_500, 50_500),
      focus: { kind: "index", index: 50_000 },
    });
    expect(action).toEqual({ kind: "idle" });
  });

  it("evicts the surplus side at the cap once the deficit is material", () => {
    // Full window 0..1000 with focus 900: coverage should be 400..1400, and the
    // after-deficit (400) exceeds the slack (chunkLimit = 100).
    const action = plan({ totalEvents: 10_000, physical: window(0, 1000), focus: { kind: "index", index: 900 } });
    expect(action).toEqual({ kind: "evict", side: "older", count: 100 });
  });

  it("does not re-center for a small drift at the cap", () => {
    const action = plan({
      totalEvents: 100_000,
      physical: window(49_450, 50_450),
      focus: { kind: "index", index: 50_000 },
    });
    expect(action).toEqual({ kind: "idle" });
  });

  it("trims the side farther from the focus when over the cap", () => {
    const action = plan({
      totalEvents: 10_000,
      physical: window(4000, 5100),
      capEvents: 1000,
      focus: { kind: "index", index: 5000 },
    });
    expect(action).toEqual({ kind: "evict", side: "older", count: 100 });
  });

  it("clamps a tail focus to the last event", () => {
    const action = plan({ totalEvents: 50, physical: window(0, 50) });
    expect(action).toEqual({ kind: "idle" });
  });
});
