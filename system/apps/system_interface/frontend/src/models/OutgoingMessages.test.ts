import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

// Mithril is mocked to a no-op redraw -- these tests exercise the pure client
// state machine, not rendering.
vi.mock("mithril", () => ({ default: { redraw: vi.fn() } }));

import {
  addOutgoing,
  clearFailedOutgoing,
  failOutgoing,
  getOutgoingMessages,
  noteBackendArrivals,
  resolveOutgoing,
} from "./OutgoingMessages";

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
    const out = getOutgoingMessages(agent);
    expect(out.map((o) => o.content)).toEqual(["first", "second"]);
    expect(out.every((o) => o.status === "sending")).toBe(true);
  });

  it("drops the oldest sending bubble when a backend arrival lands (no overlap)", () => {
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
    // An id is observed while there is nothing to drop...
    noteBackendArrivals(agent, ["real-early"]);
    // ...then the user sends. The already-seen id must not retroactively drop it.
    addOutgoing(agent, "later");
    noteBackendArrivals(agent, ["real-early"]);
    expect(getOutgoingMessages(agent).map((o) => o.content)).toEqual(["later"]);
  });

  it("flips to a persistent failed state on send failure and arrivals leave it be", () => {
    const agent = `a-${Math.random()}`;
    const id = addOutgoing(agent, "hello");
    failOutgoing(agent, id, "boom");
    const [entry] = getOutgoingMessages(agent);
    expect(entry.status).toBe("failed");
    expect(entry.error).toBe("boom");
    // Arrivals only clear "sending" bubbles, never a failed one.
    noteBackendArrivals(agent, ["real-1"]);
    expect(getOutgoingMessages(agent)).toHaveLength(1);
    // Nor does the anti-strand fallback touch it.
    vi.advanceTimersByTime(10000);
    expect(getOutgoingMessages(agent)).toHaveLength(1);
  });

  it("sweeps a delivered bubble via the fallback if no arrival is ever observed", () => {
    const agent = `a-${Math.random()}`;
    const id = addOutgoing(agent, "hello");
    resolveOutgoing(agent, id); // POST resolved, arms the anti-strand fallback
    expect(getOutgoingMessages(agent)).toHaveLength(1);
    vi.advanceTimersByTime(7000);
    expect(getOutgoingMessages(agent)).toHaveLength(0);
  });

  it("clears failed entries but keeps sending ones", () => {
    const agent = `a-${Math.random()}`;
    const failedId = addOutgoing(agent, "bad");
    failOutgoing(agent, failedId, "boom");
    addOutgoing(agent, "good");
    clearFailedOutgoing(agent);
    const out = getOutgoingMessages(agent);
    expect(out.map((o) => o.content)).toEqual(["good"]);
    expect(out[0].status).toBe("sending");
  });
});
