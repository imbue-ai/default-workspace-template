import { describe, expect, it, vi } from "vitest";

// Mithril is mocked to a no-op redraw -- these tests exercise the pure client
// state machine, not rendering.
vi.mock("mithril", () => ({ default: { redraw: vi.fn() } }));

import {
  addOutgoing,
  clearOutgoing,
  dropOutgoing,
  getOutgoingMessages,
  noteBackendArrivals,
} from "./OutgoingMessages";
import { asChatId } from "../ids";

describe("OutgoingMessages", () => {
  it("adds a sending bubble and preserves send order", () => {
    const agent = asChatId(`a-${Math.random()}`);
    addOutgoing(agent, "first");
    addOutgoing(agent, "second");
    expect(getOutgoingMessages(agent).map((o) => o.content)).toEqual(["first", "second"]);
  });

  it("drops the oldest bubble when a backend arrival lands (real first, then remove)", () => {
    const agent = asChatId(`a-${Math.random()}`);
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
    const agent = asChatId(`a-${Math.random()}`);
    addOutgoing(agent, "first");
    addOutgoing(agent, "second");
    noteBackendArrivals(agent, ["real-1"]);
    noteBackendArrivals(agent, ["real-1"]); // same id again -> no-op
    expect(getOutgoingMessages(agent).map((o) => o.content)).toEqual(["second"]);
  });

  it("does not drop a bubble for an arrival id seen before that bubble existed", () => {
    const agent = asChatId(`a-${Math.random()}`);
    noteBackendArrivals(agent, ["real-early"]); // observed with nothing to drop
    addOutgoing(agent, "later");
    noteBackendArrivals(agent, ["real-early"]); // already seen -> must not drop
    expect(getOutgoingMessages(agent).map((o) => o.content)).toEqual(["later"]);
  });

  it("drops a specific bubble on the failure path (text is returned to the composer by the caller)", () => {
    const agent = asChatId(`a-${Math.random()}`);
    const id = addOutgoing(agent, "hello");
    dropOutgoing(agent, id);
    expect(getOutgoingMessages(agent)).toHaveLength(0);
  });

  it("clears only the pre-interrupt snapshot, leaving a bubble sent during the round-trip", () => {
    const agent = asChatId(`a-${Math.random()}`);
    const first = addOutgoing(agent, "first");
    const second = addOutgoing(agent, "second");
    // Snapshot taken before the interrupt round-trip; a NEW send lands during it.
    const preInterrupt = [first, second];
    const during = addOutgoing(agent, "during");
    clearOutgoing(agent, preInterrupt);
    // The two pre-interrupt bubbles are gone (Returned to the composer / Delivered);
    // the message sent during the round-trip is untouched (not in the returned block).
    expect(getOutgoingMessages(agent).map((o) => o.content)).toEqual(["during"]);
    expect(getOutgoingMessages(agent).map((o) => o.id)).toEqual([during]);
  });

  it("clearOutgoing with no ids is a no-op", () => {
    const agent = asChatId(`a-${Math.random()}`);
    addOutgoing(agent, "only");
    clearOutgoing(agent, []);
    expect(getOutgoingMessages(agent).map((o) => o.content)).toEqual(["only"]);
  });
});
