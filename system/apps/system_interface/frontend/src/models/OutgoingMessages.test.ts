import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

// Mithril is mocked to a no-op redraw -- these tests exercise the pure client
// state machine, not rendering.
vi.mock("mithril", () => ({ default: { redraw: vi.fn() } }));

import {
  addOutgoing,
  dropOutgoing,
  getFlushFreeze,
  getOutgoingMessages,
  noteBackendArrivals,
  releaseFlushFreeze,
  resolveOutgoing,
  startFlushFreeze,
} from "./OutgoingMessages";

const QM = (queued_id: string, content: string) => ({ queued_id, content, timestamp: "t" });

describe("OutgoingMessages", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("adds a sending bubble and preserves send order", () => {
    const agent = `a-${Math.random()}`;
    addOutgoing(agent, "first");
    addOutgoing(agent, "second");
    expect(getOutgoingMessages(agent).map((o) => o.content)).toEqual(["first", "second"]);
  });

  it("drops the oldest bubble when a backend arrival lands (no overlap)", () => {
    const agent = `a-${Math.random()}`;
    addOutgoing(agent, "first");
    addOutgoing(agent, "second");
    // A real user item arrives (a transcript event_id or a queued_id) -> the
    // OLDEST bubble clears exactly as the real one appears.
    noteBackendArrivals(agent, ["real-1"]);
    expect(getOutgoingMessages(agent).map((o) => o.content)).toEqual(["second"]);
    noteBackendArrivals(agent, ["real-2"]);
    expect(getOutgoingMessages(agent)).toHaveLength(0);
  });

  it("dedupes arrival ids so a re-streamed event or re-pushed snapshot drops nothing extra", () => {
    const agent = `a-${Math.random()}`;
    addOutgoing(agent, "first");
    addOutgoing(agent, "second");
    noteBackendArrivals(agent, ["real-1"]);
    noteBackendArrivals(agent, ["real-1"]); // same id again -> no-op
    expect(getOutgoingMessages(agent).map((o) => o.content)).toEqual(["second"]);
  });

  it("does not drop a bubble for an arrival id seen before that bubble existed", () => {
    const agent = `a-${Math.random()}`;
    noteBackendArrivals(agent, ["real-early"]); // observed with nothing to drop
    addOutgoing(agent, "later");
    noteBackendArrivals(agent, ["real-early"]); // already seen -> must not drop
    expect(getOutgoingMessages(agent).map((o) => o.content)).toEqual(["later"]);
  });

  it("drops a specific bubble on the failure path (text is returned to the composer by the caller)", () => {
    const agent = `a-${Math.random()}`;
    const id = addOutgoing(agent, "hello");
    dropOutgoing(agent, id);
    expect(getOutgoingMessages(agent)).toHaveLength(0);
  });

  it("sweeps a delivered bubble via the fallback if no arrival is ever observed", () => {
    const agent = `a-${Math.random()}`;
    const id = addOutgoing(agent, "hello");
    resolveOutgoing(agent, id); // POST resolved, arms the anti-strand fallback
    expect(getOutgoingMessages(agent)).toHaveLength(1);
    vi.advanceTimersByTime(7000);
    expect(getOutgoingMessages(agent)).toHaveLength(0);
  });

  it("holds a shoulder-tap freeze and releases it on the next backend arrival", () => {
    const agent = `a-${Math.random()}`;
    startFlushFreeze(agent, [QM("q1", "one"), QM("q2", "two")]);
    expect(getFlushFreeze(agent)?.messages.map((m) => m.content)).toEqual(["one", "two"]);

    // A genuinely-new arrival (the resent message landing) releases the hold.
    noteBackendArrivals(agent, ["resent-1"]);
    expect(getFlushFreeze(agent)).toBeUndefined();
  });

  it("does not release the freeze on an already-seen arrival id", () => {
    const agent = `a-${Math.random()}`;
    noteBackendArrivals(agent, ["dup"]); // seen before the freeze exists
    startFlushFreeze(agent, [QM("q1", "one")]);
    noteBackendArrivals(agent, ["dup"]); // already seen -> no release
    expect(getFlushFreeze(agent)).toBeDefined();
    noteBackendArrivals(agent, ["fresh"]); // a new one releases
    expect(getFlushFreeze(agent)).toBeUndefined();
  });

  it("releases the freeze via the cap if no arrival is ever observed", () => {
    const agent = `a-${Math.random()}`;
    startFlushFreeze(agent, [QM("q1", "one")]);
    expect(getFlushFreeze(agent)).toBeDefined();
    vi.advanceTimersByTime(21000);
    expect(getFlushFreeze(agent)).toBeUndefined();
  });

  it("releaseFlushFreeze drops the hold (the flush-failure path)", () => {
    const agent = `a-${Math.random()}`;
    startFlushFreeze(agent, [QM("q1", "one")]);
    releaseFlushFreeze(agent);
    expect(getFlushFreeze(agent)).toBeUndefined();
  });
});
