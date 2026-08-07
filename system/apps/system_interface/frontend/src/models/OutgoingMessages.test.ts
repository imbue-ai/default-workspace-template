import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

// Mithril is mocked to a no-op redraw -- these tests exercise the pure client
// state machine, not rendering.
vi.mock("mithril", () => ({ default: { redraw: vi.fn() } }));

import {
  addOutgoing,
  clearFailedOutgoing,
  failOutgoing,
  getOutgoingMessages,
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

  it("removes a bubble a short beat after the send resolves", () => {
    const agent = `a-${Math.random()}`;
    const id = addOutgoing(agent, "hello");
    resolveOutgoing(agent, id);
    // Still shown immediately (the settle beat lets the real bubble render first).
    expect(getOutgoingMessages(agent)).toHaveLength(1);
    vi.advanceTimersByTime(1000);
    expect(getOutgoingMessages(agent)).toHaveLength(0);
  });

  it("flips to a persistent failed state on send failure", () => {
    const agent = `a-${Math.random()}`;
    const id = addOutgoing(agent, "hello");
    failOutgoing(agent, id, "boom");
    const [entry] = getOutgoingMessages(agent);
    expect(entry.status).toBe("failed");
    expect(entry.error).toBe("boom");
    // A failed bubble is NOT swept by the resolve timer.
    vi.advanceTimersByTime(5000);
    expect(getOutgoingMessages(agent)).toHaveLength(1);
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

  it("resolving an unknown id is a harmless no-op", () => {
    const agent = `a-${Math.random()}`;
    addOutgoing(agent, "hello");
    resolveOutgoing(agent, "outgoing-does-not-exist");
    vi.advanceTimersByTime(1000);
    // The real entry is untouched.
    expect(getOutgoingMessages(agent)).toHaveLength(1);
  });
});
