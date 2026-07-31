import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mithril is mocked: request is driven per-test, redraw is a no-op spy. This
// keeps the store unit-testable without a DOM or a real network.
const { mockRequest, mockRedraw } = vi.hoisted(() => ({
  mockRequest: vi.fn(),
  mockRedraw: vi.fn(),
}));
vi.mock("mithril", () => ({
  default: { request: mockRequest, redraw: mockRedraw },
}));

import {
  appendEvents,
  clearConversationNotFound,
  isConversationNotFound,
  prependEvents,
  evictOldEvents,
  fetchEvents,
  fetchBackfillEvents,
  fetchForwardEvents,
  fetchWindowAtOffset,
  getEventsForAgent,
  getEventCount,
  getFirstEventId,
  getFirstOffset,
  getRenderVersion,
  getTotalEventCount,
  hasMoreBefore,
  hasMoreAfter,
  MAX_HELD_EVENTS,
  EVICT_TARGET_EVENTS,
  PAGING_REQUEST_TIMEOUT_MS,
  type AssistantMessageEvent,
  type ToolCall,
  type TranscriptEvent,
} from "./Response";

function makeEvent(id: string): TranscriptEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "user_message",
    event_id: id,
    source: "test",
    message_uuid: id,
    role: "user",
    content: id,
  };
}

function assistantWithAgentToolCall(
  eventId: string,
  toolCallId: string,
  metadata?: { agent_type: string; description: string; session_id: string },
): AssistantMessageEvent {
  return {
    timestamp: "2026-01-01T00:00:01Z",
    type: "assistant_message",
    event_id: eventId,
    source: "claude/common_transcript",
    message_uuid: eventId,
    model: "test-model",
    text: "",
    tool_calls: [
      {
        tool_call_id: toolCallId,
        tool_name: "Agent",
        input_preview: "{}",
        ...(metadata ? { subagent_metadata: metadata } : {}),
      },
    ],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
  };
}

// getEventsForAgent returns the TranscriptEvent union; narrow to the assistant
// variant before touching tool_calls (the discriminated-union contract).
function toolCallsOf(event: TranscriptEvent): ToolCall[] {
  if (event.type !== "assistant_message") {
    throw new Error(`expected assistant_message, got ${event.type}`);
  }
  return event.tool_calls;
}

let counter = 0;
function freshAgent(): string {
  return `agent-${counter++}`;
}

beforeEach(() => {
  // base-path reads a <meta> tag via document.querySelector when building URLs.
  globalThis.document = { querySelector: () => null } as unknown as Document;
  mockRequest.mockReset();
  mockRedraw.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

function ids(agentId: string): string[] {
  return getEventsForAgent(agentId).map((e) => e.event_id);
}

describe("appendEvents subagent_metadata merge", () => {
  it("merges late subagent_metadata onto an already-stored assistant message", () => {
    const agentId = freshAgent();
    const metadata = { agent_type: "Explore", description: "explore foo", session_id: "agent-sub1" };

    // Parent Agent tool_call streamed before its subagent linkage was known.
    appendEvents(agentId, [assistantWithAgentToolCall("ev-1", "toolu_1")]);
    const before = getEventsForAgent(agentId);
    expect(before).toHaveLength(1);
    expect(toolCallsOf(before[0])[0].subagent_metadata).toBeUndefined();

    // Backend re-broadcasts the same event (same event_id) once linkage lands.
    appendEvents(agentId, [assistantWithAgentToolCall("ev-1", "toolu_1", metadata)]);

    const after = getEventsForAgent(agentId);
    // Still a single message -- the re-broadcast must not be appended as a duplicate.
    expect(after).toHaveLength(1);
    expect(toolCallsOf(after[0])[0].subagent_metadata).toEqual(metadata);
  });

  it("ignores a re-broadcast that carries no new metadata", () => {
    const agentId = freshAgent();
    appendEvents(agentId, [assistantWithAgentToolCall("ev-1", "toolu_1")]);
    appendEvents(agentId, [assistantWithAgentToolCall("ev-1", "toolu_1")]);

    const events = getEventsForAgent(agentId);
    expect(events).toHaveLength(1);
    expect(toolCallsOf(events[0])[0].subagent_metadata).toBeUndefined();
  });

  it("still appends genuinely new events", () => {
    const agentId = freshAgent();
    appendEvents(agentId, [assistantWithAgentToolCall("ev-1", "toolu_1")]);
    appendEvents(agentId, [assistantWithAgentToolCall("ev-2", "toolu_2")]);

    expect(getEventsForAgent(agentId)).toHaveLength(2);
  });
});

describe("dedup", () => {
  it("appendEvents ignores ids already present", () => {
    const agent = freshAgent();
    appendEvents(agent, [makeEvent("a"), makeEvent("b")]);
    appendEvents(agent, [makeEvent("b"), makeEvent("c")]);
    expect(ids(agent)).toEqual(["a", "b", "c"]);
  });

  it("prependEvents ignores ids already present and keeps order", () => {
    const agent = freshAgent();
    appendEvents(agent, [makeEvent("c"), makeEvent("d")]);
    prependEvents(agent, [makeEvent("a"), makeEvent("b"), makeEvent("c")]);
    expect(ids(agent)).toEqual(["a", "b", "c", "d"]);
  });
});

// The loaded window's position in the full transcript is tracked by offset (the
// global index of its first event) + total. "More above" is offset > 0, "more
// below" is offset + held < total -- the client derives both, replacing has_more.
describe("window position (offset / total)", () => {
  it("fetchEvents records offset and total from the server", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("x")], offset: 5, total: 10 });
    await fetchEvents(agent);
    expect(getFirstOffset(agent)).toBe(5);
    expect(getTotalEventCount(agent)).toBe(10);
    expect(hasMoreBefore(agent)).toBe(true); // offset 5 > 0
    expect(hasMoreAfter(agent)).toBe(true); // 5 + 1 < 10
  });

  it("treats a response without offset/total as a complete window", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("x")] });
    await fetchEvents(agent);
    expect(getFirstOffset(agent)).toBe(0);
    expect(hasMoreBefore(agent)).toBe(false);
    expect(hasMoreAfter(agent)).toBe(false);
  });

  it("backfill stops once the window reaches the start", async () => {
    const agent = freshAgent();
    // Window holds [b, c] starting at index 1, so one older event (a) exists.
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("b"), makeEvent("c")], offset: 1, total: 3 });
    await fetchEvents(agent);
    expect(hasMoreBefore(agent)).toBe(true);

    // The older page brings the window start to 0.
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("a")], offset: 0, total: 3 });
    await fetchBackfillEvents(agent);
    expect(ids(agent)).toEqual(["a", "b", "c"]);
    expect(getFirstOffset(agent)).toBe(0);
    expect(hasMoreBefore(agent)).toBe(false);

    // A subsequent backfill must not hit the network at all.
    mockRequest.mockClear();
    await fetchBackfillEvents(agent);
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("backfill pages before the first held event", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e5")], offset: 5, total: 8 });
    await fetchEvents(agent);

    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e3"), makeEvent("e4")], offset: 3, total: 8 });
    await fetchBackfillEvents(agent);

    const call = mockRequest.mock.calls[mockRequest.mock.calls.length - 1][0];
    expect(call.params.before).toBe("e5");
    expect(ids(agent)).toEqual(["e3", "e4", "e5"]);
    expect(getFirstOffset(agent)).toBe(3);
  });

  it("forward-pages newer events after a window moved off the tail", async () => {
    const agent = freshAgent();
    // A window in the middle: holds [m2, m3] at offset 2 of 6, so newer exist.
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("m2"), makeEvent("m3")], offset: 2, total: 6 });
    await fetchEvents(agent);
    expect(hasMoreAfter(agent)).toBe(true);

    mockRequest.mockResolvedValueOnce({ events: [makeEvent("m4"), makeEvent("m5")], offset: 4, total: 6 });
    await fetchForwardEvents(agent);

    const call = mockRequest.mock.calls[mockRequest.mock.calls.length - 1][0];
    expect(call.params.after).toBe("m3"); // cursor is the last held event
    expect(ids(agent)).toEqual(["m2", "m3", "m4", "m5"]);
    expect(hasMoreAfter(agent)).toBe(false); // window now reaches the tail

    // No newer history left, so a further forward page makes no request.
    mockRequest.mockClear();
    await fetchForwardEvents(agent);
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("jumps the window to an arbitrary offset, replacing held events", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("tail")], offset: 99, total: 100 });
    await fetchEvents(agent);

    mockRequest.mockResolvedValueOnce({ events: [makeEvent("mid")], offset: 40, total: 100 });
    await fetchWindowAtOffset(agent, 40);

    const call = mockRequest.mock.calls[mockRequest.mock.calls.length - 1][0];
    expect(call.params.offset).toBe("40");
    expect(ids(agent)).toEqual(["mid"]); // window replaced, not appended
    expect(getFirstOffset(agent)).toBe(40);
    expect(hasMoreBefore(agent)).toBe(true);
    expect(hasMoreAfter(agent)).toBe(true);
  });
});

interface PendingRequest {
  promise: Promise<unknown>;
  resolve: (value: unknown) => void;
}

/** A request the test resolves by hand, so another fetch can be interleaved
 *  while it is in flight. */
function pendingRequest(): PendingRequest {
  let resolve!: (value: unknown) => void;
  const promise = new Promise<unknown>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

// The paging fences. Each of these guards one step of the reproduced wedge: a
// backfill in flight across an SSE-reconnect snapshot reset landed a
// non-contiguous older page carrying its old-epoch offset, which left
// `firstOffset + held < total` while the window's last event WAS the tail, so
// the panel re-issued `after=<tail>` forever, and a later hung request pinned
// the in-flight guard and froze the panel entirely.
describe("paging fences", () => {
  it("discards an older page whose window was replaced while it was in flight", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e10")], offset: 10, total: 20 });
    await fetchEvents(agent);

    // Backfill goes out asking for the events before e10.
    const backfill = pendingRequest();
    mockRequest.mockReturnValueOnce(backfill.promise);
    const backfillDone = fetchBackfillEvents(agent);

    // An SSE reconnect refetches the snapshot mid-flight, resetting the window
    // one event earlier.
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e9")], offset: 9, total: 20 });
    await fetchEvents(agent);

    // The in-flight page lands. Its coordinates happen to abut the NEW window, so
    // the contiguity guard alone would wave it through -- but it answers a
    // question about the window that no longer exists, and splicing it in would
    // put the event before e10 directly before e9.
    backfill.resolve({ events: [makeEvent("e8")], offset: 8, total: 20 });
    await backfillDone;

    expect(ids(agent)).toEqual(["e9"]);
    expect(getFirstOffset(agent)).toBe(9);
  });

  it("drops an older page that does not abut the window start", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e10")], offset: 10, total: 20 });
    await fetchEvents(agent);

    // Claims to start at 3 and holds one event, so it ends at 4 -- nowhere near
    // the window start at 10. Gluing it on would make the window claim events 5-9
    // that it does not hold.
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e3")], offset: 3, total: 20 });
    await fetchBackfillEvents(agent);

    expect(ids(agent)).toEqual(["e10"]);
    expect(getFirstOffset(agent)).toBe(10);
    // The window start is untouched, so the next scroll retries from current
    // coordinates rather than being stuck behind a corrupted window.
    expect(hasMoreBefore(agent)).toBe(true);
  });

  it("accepts an older page that abuts the window start", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e10")], offset: 10, total: 20 });
    await fetchEvents(agent);

    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e8"), makeEvent("e9")], offset: 8, total: 20 });
    await fetchBackfillEvents(agent);

    expect(ids(agent)).toEqual(["e8", "e9", "e10"]);
    expect(getFirstOffset(agent)).toBe(8);
  });

  it("drops a newer page that does not abut the window end", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e2"), makeEvent("e3")], offset: 2, total: 20 });
    await fetchEvents(agent);

    // The window ends at 4; a page starting at 9 leaves a hole.
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e9")], offset: 9, total: 20 });
    await fetchForwardEvents(agent);

    expect(ids(agent)).toEqual(["e2", "e3"]);
    expect(hasMoreAfter(agent)).toBe(true);
  });

  it("snaps the window onto the tail when the server reports nothing after our last event", async () => {
    const agent = freshAgent();
    // A window whose start is stale: it holds the last two events of the
    // transcript but thinks it starts at 100, so `firstOffset + held < total`.
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e589"), makeEvent("e590")], offset: 100, total: 591 });
    await fetchEvents(agent);
    expect(hasMoreAfter(agent)).toBe(true);

    // The exact answer the live server gave the wedged client.
    mockRequest.mockResolvedValueOnce({ events: [], offset: 591, total: 591 });
    await fetchForwardEvents(agent);

    expect(getFirstOffset(agent)).toBe(589);
    expect(getTotalEventCount(agent)).toBe(591);
    expect(hasMoreAfter(agent)).toBe(false);

    // ...and therefore the same cursor is never queried again.
    mockRequest.mockClear();
    await fetchForwardEvents(agent);
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("reloads the snapshot when the held window cannot be squared with the reported tail", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({
      events: [makeEvent("a"), makeEvent("b"), makeEvent("c")],
      offset: 0,
      total: 10,
    });
    await fetchEvents(agent);

    // Holding three events but the whole transcript is now two: unreconcilable.
    mockRequest.mockResolvedValueOnce({ events: [], offset: 2, total: 2 });
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("x"), makeEvent("y")], offset: 0, total: 2 });
    await fetchForwardEvents(agent);

    expect(ids(agent)).toEqual(["x", "y"]);
    expect(getFirstOffset(agent)).toBe(0);
    expect(getTotalEventCount(agent)).toBe(2);
  });

  it("timeboxes every paging request and survives one timing out", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e5")], offset: 5, total: 10 });
    await fetchEvents(agent);

    mockRequest.mockRejectedValueOnce(new Error("Request timed out"));
    // Must settle rather than hang: the caller releases its in-flight guard in a
    // `finally`, so a request that never settles freezes the panel.
    await fetchBackfillEvents(agent);

    for (const [options] of mockRequest.mock.calls) {
      expect(options.timeout).toBe(PAGING_REQUEST_TIMEOUT_MS);
    }
    // A failed page leaves the window exactly where it was.
    expect(ids(agent)).toEqual(["e5"]);
    expect(getFirstOffset(agent)).toBe(5);
  });

  it("reports a failed offset jump so the caller does not pin to a window that never landed", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("tail")], offset: 99, total: 100 });
    await fetchEvents(agent);

    mockRequest.mockRejectedValueOnce(new Error("Request timed out"));
    expect(await fetchWindowAtOffset(agent, 40)).toBe(false);
    expect(ids(agent)).toEqual(["tail"]);

    mockRequest.mockResolvedValueOnce({ events: [makeEvent("mid")], offset: 40, total: 100 });
    expect(await fetchWindowAtOffset(agent, 40)).toBe(true);
  });
});

// A 404 from /events is ambiguous: the agent may be genuinely destroyed, or the
// `mngr observe` pipeline may just be restarting. The store therefore records the
// marker but never makes it permanent -- the chat panel clears it when the agent
// reappears in an agents_updated snapshot, and the refetch has to behave like a
// first load.
describe("conversation not-found marker", () => {
  it("is set by a 404 and cleared for a retry that then loads normally", async () => {
    const agent = freshAgent();
    mockRequest.mockRejectedValueOnce({ code: 404, message: "Agent not found" });
    await expect(fetchEvents(agent)).rejects.toBeTruthy();
    expect(isConversationNotFound(agent)).toBe(true);

    clearConversationNotFound(agent);
    expect(isConversationNotFound(agent)).toBe(false);

    mockRequest.mockResolvedValueOnce({ events: [makeEvent("a")], offset: 0, total: 1 });
    await fetchEvents(agent);
    expect(ids(agent)).toEqual(["a"]);
    expect(isConversationNotFound(agent)).toBe(false);
  });

  it("is not set by a non-404 failure", async () => {
    const agent = freshAgent();
    mockRequest.mockRejectedValueOnce({ code: 500, message: "boom" });
    await expect(fetchEvents(agent)).rejects.toBeTruthy();
    expect(isConversationNotFound(agent)).toBe(false);
  });
});

describe("evictOldEvents", () => {
  it("does nothing below the cap", () => {
    const agent = freshAgent();
    appendEvents(
      agent,
      Array.from({ length: 10 }, (_v, i) => makeEvent(`e${i}`)),
    );
    expect(evictOldEvents(agent)).toBe(0);
    expect(getEventCount(agent)).toBe(10);
  });

  it("trims the oldest down to the target and flags more history", () => {
    const agent = freshAgent();
    const events = Array.from({ length: MAX_HELD_EVENTS + 200 }, (_v, i) => makeEvent(`e${i}`));
    appendEvents(agent, events);

    const removed = evictOldEvents(agent);
    expect(removed).toBe(events.length - EVICT_TARGET_EVENTS);
    expect(getEventCount(agent)).toBe(EVICT_TARGET_EVENTS);
    // The oldest are gone; the newest are kept.
    expect(getFirstEventId(agent)).toBe(`e${removed}`);
    // The window start advanced past the dropped events, so older history is once
    // again reachable above -- the evicted events can be paged back in.
    expect(getFirstOffset(agent)).toBe(removed);
    expect(hasMoreBefore(agent)).toBe(true);
  });

  it("re-admits evicted ids on a later prepend (dedup index was pruned)", () => {
    const agent = freshAgent();
    const events = Array.from({ length: MAX_HELD_EVENTS + 50 }, (_v, i) => makeEvent(`e${i}`));
    appendEvents(agent, events);
    const removed = evictOldEvents(agent);
    // Re-fetching an evicted event prepends it again rather than being deduped away.
    const reFetched = makeEvent("e0");
    prependEvents(agent, [reFetched]);
    expect(getFirstEventId(agent)).toBe("e0");
    expect(removed).toBeGreaterThan(0);
  });
});

// The chat view memoizes its (expensive) turn-grouping keyed on this version, so
// the contract that matters is: every mutation that changes what renders bumps
// it, and a no-op mutation does not. A missed bump would leave the view showing
// stale grouping; a spurious bump would defeat the scroll-time caching.
describe("render version", () => {
  it("bumps on a real append but not on a duplicate", () => {
    const agent = freshAgent();
    const v0 = getRenderVersion(agent);
    appendEvents(agent, [makeEvent("a")]);
    const v1 = getRenderVersion(agent);
    expect(v1).toBeGreaterThan(v0);
    // Re-appending the same event is a no-op and must not bump.
    appendEvents(agent, [makeEvent("a")]);
    expect(getRenderVersion(agent)).toBe(v1);
  });

  it("bumps when a re-broadcast upgrades a held event in place", () => {
    const agent = freshAgent();
    appendEvents(agent, [assistantWithAgentToolCall("e", "call-1")]);
    const v1 = getRenderVersion(agent);
    // Same event_id, now carrying subagent metadata: merged in place, so the
    // array reference is unchanged but the version must still bump.
    appendEvents(agent, [
      assistantWithAgentToolCall("e", "call-1", {
        agent_type: "Explore",
        description: "look",
        session_id: "sub-1",
      }),
    ]);
    expect(getRenderVersion(agent)).toBeGreaterThan(v1);
  });

  it("bumps on prepend and on eviction", () => {
    const agent = freshAgent();
    appendEvents(
      agent,
      Array.from({ length: MAX_HELD_EVENTS + 50 }, (_v, i) => makeEvent(`e${i}`)),
    );
    const vBeforePrepend = getRenderVersion(agent);
    prependEvents(agent, [makeEvent("older")]);
    const vAfterPrepend = getRenderVersion(agent);
    expect(vAfterPrepend).toBeGreaterThan(vBeforePrepend);
    evictOldEvents(agent);
    expect(getRenderVersion(agent)).toBeGreaterThan(vAfterPrepend);
  });

  it("bumps on a fetch (window reset)", async () => {
    const agent = freshAgent();
    const v0 = getRenderVersion(agent);
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("x")], offset: 0, total: 1 });
    await fetchEvents(agent);
    expect(getRenderVersion(agent)).toBeGreaterThan(v0);
  });

  // An older/newer page that comes back empty does not change the held events but
  // does reconcile the window bounds (the server reports the window already sits at
  // an edge), so it must still bump -- the scrollbar geometry the view derives from
  // those bounds has changed. These edge-reconciliation paths write the store
  // directly (no event delta), so they are the easiest place to forget the bump.
  it("bumps when an empty backfill page snaps the window start to the beginning", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e2")], offset: 2, total: 5 });
    await fetchEvents(agent);
    expect(hasMoreBefore(agent)).toBe(true);
    const vBefore = getRenderVersion(agent);

    // Server reports nothing before the cursor: the window already starts at 0.
    mockRequest.mockResolvedValueOnce({ events: [], total: 5 });
    await fetchBackfillEvents(agent);

    expect(getFirstOffset(agent)).toBe(0);
    expect(hasMoreBefore(agent)).toBe(false);
    expect(getRenderVersion(agent)).toBeGreaterThan(vBefore);
  });

  it("bumps when an empty forward page corrects total down to the tail", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("m2"), makeEvent("m3")], offset: 2, total: 6 });
    await fetchEvents(agent);
    expect(hasMoreAfter(agent)).toBe(true);
    const vBefore = getRenderVersion(agent);

    // Server reports nothing after the cursor and a smaller total: the window now
    // reaches the live tail.
    mockRequest.mockResolvedValueOnce({ events: [], total: 4 });
    await fetchForwardEvents(agent);

    expect(getTotalEventCount(agent)).toBe(4);
    expect(hasMoreAfter(agent)).toBe(false);
    expect(getRenderVersion(agent)).toBeGreaterThan(vBefore);
  });
});

// `total` lets the chat view size the scrollbar for the whole conversation while
// only a window is held. It reflects the server's count, and never drops below
// the loaded window's end so the window always fits inside it.
describe("total event count", () => {
  it("reports the server total when it exceeds the held window", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("x")], offset: 100, total: 500 });
    await fetchEvents(agent);
    expect(getTotalEventCount(agent)).toBe(500);
  });

  it("falls back to the held count when the server omits total", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("a"), makeEvent("b")] });
    await fetchEvents(agent);
    expect(getTotalEventCount(agent)).toBe(2);
  });
});
