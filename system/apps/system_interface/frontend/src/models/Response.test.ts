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
  addMessageSentListener,
  appendEvents,
  appendForwardEvents,
  prependEvents,
  evictEvents,
  removeMessageSentListener,
  sendMessage,
  fetchEvents,
  fetchBackfillEvents,
  fetchForwardEvents,
  fetchWindowAtOffset,
  getConversationLoadState,
  getEventsForAgent,
  getEventCount,
  getFirstEventId,
  getLastEventId,
  getFirstOffset,
  getRenderVersion,
  getTotalEventCount,
  getEventDetailState,
  getEventDetailVersion,
  requestEventDetail,
  hasMoreBefore,
  hasMoreAfter,
  isConversationNotFound,
  type AssistantMessageEvent,
  type ToolCall,
  type TranscriptEvent,
} from "./Response";
import { asChatId, type ChatId } from "../ids";

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
        input_chars: 2,
        ...(metadata ? { subagent_metadata: metadata } : {}),
      },
    ],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
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
function freshAgent(): ChatId {
  return asChatId(`agent-${counter++}`);
}

/** A request whose settling the test controls, for observing the in-flight state and ordering two fetches. */
function deferredResponse(): {
  promise: Promise<unknown>;
  resolve: (value: unknown) => void;
  reject: (reason: unknown) => void;
} {
  let resolve!: (value: unknown) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<unknown>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
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

function ids(chatId: ChatId): string[] {
  return getEventsForAgent(chatId).map((e) => e.event_id);
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

  it("replaces an already-held event in place when a re-broadcast changes its content", () => {
    // The spine's supersession path: the backend re-broadcasts a held event (same id)
    // with updated content; the store upgrades it in place rather than dropping it as a
    // duplicate or appending a second copy.
    const agent = freshAgent();
    const userEvent = (id: string, content: string): TranscriptEvent => ({
      timestamp: "2026-01-01T00:00:00Z",
      type: "user_message",
      event_id: id,
      source: "test",
      message_uuid: id,
      role: "user",
      content,
    });
    const contentOf = (id: string) =>
      getEventsForAgent(agent)
        .filter((e) => e.event_id === id)
        .map((e) => (e as { content?: string }).content);
    appendEvents(agent, [userEvent("a", "first"), makeEvent("b")]);
    expect(contentOf("a")).toEqual(["first"]);

    appendEvents(agent, [userEvent("a", "first, corrected")]);
    // Still two events, same order; "a" upgraded in place.
    expect(ids(agent)).toEqual(["a", "b"]);
    expect(contentOf("a")).toEqual(["first, corrected"]);
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
    await fetchBackfillEvents(agent, 50);
    expect(ids(agent)).toEqual(["a", "b", "c"]);
    expect(getFirstOffset(agent)).toBe(0);
    expect(hasMoreBefore(agent)).toBe(false);

    // A subsequent backfill must not hit the network at all.
    mockRequest.mockClear();
    await fetchBackfillEvents(agent, 50);
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("backfill pages before the first held event", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e5")], offset: 5, total: 8 });
    await fetchEvents(agent);

    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e3"), makeEvent("e4")], offset: 3, total: 8 });
    await fetchBackfillEvents(agent, 50);

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
    await fetchForwardEvents(agent, 50);

    const call = mockRequest.mock.calls[mockRequest.mock.calls.length - 1][0];
    expect(call.params.after).toBe("m3"); // cursor is the last held event
    expect(ids(agent)).toEqual(["m2", "m3", "m4", "m5"]);
    expect(hasMoreAfter(agent)).toBe(false); // window now reaches the tail

    // No newer history left, so a further forward page makes no request.
    mockRequest.mockClear();
    await fetchForwardEvents(agent, 50);
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("snaps the window to the tail when a forward page returns empty", async () => {
    const agent = freshAgent();
    // A window whose count arithmetic says it falls short of the total -- the
    // state left behind when events were missed during an outage (SSE
    // reconnected but the snapshot refetch failed).
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("x")], offset: 5, total: 10 });
    await fetchEvents(agent);
    expect(hasMoreAfter(agent)).toBe(true);

    // The server authoritatively answers "nothing after your last event": the
    // held window IS the live tail. Without snapping firstOffset, hasMoreAfter
    // would stick true forever -- forward paging refires with no possible
    // progress and append() drops every future live event (frozen transcript).
    mockRequest.mockResolvedValueOnce({ events: [], offset: 10, total: 10 });
    await fetchForwardEvents(agent, 50);
    expect(hasMoreAfter(agent)).toBe(false);
    expect(getFirstOffset(agent)).toBe(9);
    expect(getTotalEventCount(agent)).toBe(10);

    appendEvents(agent, [makeEvent("live")]);
    expect(ids(agent)).toEqual(["x", "live"]);
  });

  it("discards a stale backfill page that does not reach the window start", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e5"), makeEvent("e6")], offset: 5, total: 10 });
    await fetchEvents(agent);

    // A page from a replaced window's coordinates ends at index 3 < 5: gluing
    // it on would corrupt the offset arithmetic, so it must be dropped.
    prependEvents(agent, [makeEvent("s1"), makeEvent("s2")], 1, 10);
    expect(ids(agent)).toEqual(["e5", "e6"]);
    expect(getFirstOffset(agent)).toBe(5);

    // An abutting page still merges normally.
    prependEvents(agent, [makeEvent("e3"), makeEvent("e4")], 3, 10);
    expect(ids(agent)).toEqual(["e3", "e4", "e5", "e6"]);
    expect(getFirstOffset(agent)).toBe(3);
  });

  it("discards a backfill response that lands after the window start moved", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("b"), makeEvent("c")], offset: 4, total: 8 });
    await fetchEvents(agent);

    // While the backfill is in flight, the window start changes (e.g. a
    // reconnect snapshot or another page landed). The response was issued
    // against the old cursor, so it must be discarded.
    mockRequest.mockImplementationOnce(async () => {
      prependEvents(agent, [makeEvent("a")], 3, 8);
      return { events: [makeEvent("z1"), makeEvent("z2")], offset: 2, total: 8 };
    });
    await fetchBackfillEvents(agent, 50);
    expect(ids(agent)).toEqual(["a", "b", "c"]);
    expect(getFirstOffset(agent)).toBe(3);
  });

  it("discards a forward page that lands after the tail advanced", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("m2"), makeEvent("m3")], offset: 2, total: 6 });
    await fetchEvents(agent);
    expect(hasMoreAfter(agent)).toBe(true);

    // The tail advances while the forward page is in flight; the response's
    // cursor is stale, so it must be discarded (the next page refires against
    // the current tail).
    mockRequest.mockImplementationOnce(async () => {
      appendForwardEvents(agent, [makeEvent("m4")], 6);
      return { events: [makeEvent("m4-dup"), makeEvent("m5")], offset: 4, total: 6 };
    });
    await fetchForwardEvents(agent, 50);
    expect(ids(agent)).toEqual(["m2", "m3", "m4"]);
  });

  it("jumps the window to an arbitrary offset, replacing held events", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("tail")], offset: 99, total: 100 });
    await fetchEvents(agent);

    mockRequest.mockResolvedValueOnce({ events: [makeEvent("mid")], offset: 40, total: 100 });
    await fetchWindowAtOffset(agent, 40, 50);

    const call = mockRequest.mock.calls[mockRequest.mock.calls.length - 1][0];
    expect(call.params.offset).toBe("40");
    expect(ids(agent)).toEqual(["mid"]); // window replaced, not appended
    expect(getFirstOffset(agent)).toBe(40);
    expect(hasMoreBefore(agent)).toBe(true);
    expect(hasMoreAfter(agent)).toBe(true);
  });
});

describe("evictEvents", () => {
  it("does nothing for a zero or negative count", () => {
    const agent = freshAgent();
    appendEvents(
      agent,
      Array.from({ length: 10 }, (_v, i) => makeEvent(`e${i}`)),
    );
    expect(evictEvents(agent, "older", 0)).toBe(0);
    expect(evictEvents(agent, "newer", -5)).toBe(0);
    expect(getEventCount(agent)).toBe(10);
  });

  it("trims the oldest and flags more history above", () => {
    const agent = freshAgent();
    appendEvents(
      agent,
      Array.from({ length: 300 }, (_v, i) => makeEvent(`e${i}`)),
    );

    const removed = evictEvents(agent, "older", 100);
    expect(removed).toBe(100);
    expect(getEventCount(agent)).toBe(200);
    // The oldest are gone; the newest are kept.
    expect(getFirstEventId(agent)).toBe("e100");
    // The window start advanced past the dropped events, so older history is once
    // again reachable above -- the evicted events can be paged back in.
    expect(getFirstOffset(agent)).toBe(100);
    expect(hasMoreBefore(agent)).toBe(true);
  });

  it("trims the newest, pulling the window off the live tail", () => {
    const agent = freshAgent();
    appendEvents(
      agent,
      Array.from({ length: 300 }, (_v, i) => makeEvent(`e${i}`)),
    );

    const removed = evictEvents(agent, "newer", 50);
    expect(removed).toBe(50);
    expect(getEventCount(agent)).toBe(250);
    expect(getLastEventId(agent)).toBe("e249");
    expect(getFirstOffset(agent)).toBe(0);
    // The evicted newer events remain on the server, reachable by forward paging.
    expect(hasMoreAfter(agent)).toBe(true);
  });

  it("clamps the count to the held window", () => {
    const agent = freshAgent();
    appendEvents(agent, [makeEvent("only")]);
    expect(evictEvents(agent, "older", 10)).toBe(1);
    expect(getEventCount(agent)).toBe(0);
  });

  it("re-admits evicted ids on a later prepend (dedup index was pruned)", () => {
    const agent = freshAgent();
    appendEvents(
      agent,
      Array.from({ length: 100 }, (_v, i) => makeEvent(`e${i}`)),
    );
    const removed = evictEvents(agent, "older", 10);
    // Re-fetching an evicted event prepends it again rather than being deduped away.
    const reFetched = makeEvent("e0");
    prependEvents(agent, [reFetched]);
    expect(getFirstEventId(agent)).toBe("e0");
    expect(removed).toBe(10);
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
      Array.from({ length: 100 }, (_v, i) => makeEvent(`e${i}`)),
    );
    const vBeforePrepend = getRenderVersion(agent);
    prependEvents(agent, [makeEvent("older")]);
    const vAfterPrepend = getRenderVersion(agent);
    expect(vAfterPrepend).toBeGreaterThan(vBeforePrepend);
    evictEvents(agent, "older", 10);
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
    await fetchBackfillEvents(agent, 50);

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
    await fetchForwardEvents(agent, 50);

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

// Where the snapshot load stands is recorded against the agent rather than held
// by the panel that asked for it, so that a reload from anywhere -- the tab's
// Refresh, the stream's background reconnect -- moves the panel out of whatever
// state the last attempt left it in.
describe("snapshot load state", () => {
  // What mithril rejects with when the proxy answers a 503 whose body is not
  // JSON: `responseType: "json"` leaves `response` null, reading `responseText`
  // throws, so mithril builds `new Error(null)` -- whose `.message` is the
  // string "null". That is the shape that used to reach the user as
  // "Error: null".
  function proxyUnavailableError(): Error {
    return Object.assign(new Error(String(null)), { code: 503, response: null });
  }

  it("records a message naming the status when the body carries no detail", async () => {
    const agent = freshAgent();
    mockRequest.mockRejectedValueOnce(proxyUnavailableError());
    await expect(fetchEvents(agent)).rejects.toThrow();
    expect(getConversationLoadState(agent)).toEqual({ phase: "error", error: "request failed (HTTP 503)" });
  });

  it("prefers the server's own detail when the body has one", async () => {
    const agent = freshAgent();
    mockRequest.mockRejectedValueOnce(
      Object.assign(new Error("{}"), { code: 404, response: { detail: "Agent 'x' not found" } }),
    );
    await expect(fetchEvents(agent)).rejects.toThrow();
    expect(getConversationLoadState(agent).error).toBe("Agent 'x' not found");
  });

  it("reads as loading while the fetch is in flight, so nothing reports an empty transcript", async () => {
    const agent = freshAgent();
    const pending = deferredResponse();
    mockRequest.mockReturnValueOnce(pending.promise);

    const inFlight = fetchEvents(agent);
    expect(getConversationLoadState(agent).phase).toBe("loading");

    pending.resolve({ events: [makeEvent("a")] });
    await inFlight;
    expect(getConversationLoadState(agent)).toEqual({ phase: "idle", error: null });
  });

  it("settles on the next successful fetch, whoever makes it", async () => {
    const agent = freshAgent();
    mockRequest.mockRejectedValueOnce(proxyUnavailableError());
    await expect(fetchEvents(agent)).rejects.toThrow();
    expect(getConversationLoadState(agent).phase).toBe("error");

    mockRequest.mockResolvedValueOnce({ events: [makeEvent("a")] });
    await fetchEvents(agent);
    expect(getConversationLoadState(agent)).toEqual({ phase: "idle", error: null });
  });

  it("keeps the newest attempt's outcome when an older one settles after it", async () => {
    // Three callers fetch the same snapshot -- the panel's load, the tab's
    // Refresh, the stream's reconnect -- so two can be in flight at once, and a
    // request hung on a dead tunnel settles up to the 30s timeout after a later
    // one already landed. Its failure must not put the panel back on an error
    // screen for a transcript that has since loaded.
    const agent = freshAgent();
    const hung = deferredResponse();
    mockRequest.mockReturnValueOnce(hung.promise);
    const stale = fetchEvents(agent);

    mockRequest.mockResolvedValueOnce({ events: [makeEvent("a")] });
    await fetchEvents(agent);
    expect(getConversationLoadState(agent)).toEqual({ phase: "idle", error: null });

    hung.reject(proxyUnavailableError());
    await expect(stale).rejects.toThrow();
    expect(getConversationLoadState(agent)).toEqual({ phase: "idle", error: null });
  });

  it("does not let a superseded attempt's 404 declare the conversation missing", async () => {
    // The panel acts on this harder than on the load state: "No conversation
    // data" renders ahead of it, ungated by whether a transcript is on screen,
    // and the live stream is disconnected. A late 404 from an attempt a later
    // one has already answered must not blank a chat that is loaded and live.
    const agent = freshAgent();
    const hung = deferredResponse();
    mockRequest.mockReturnValueOnce(hung.promise);
    const stale = fetchEvents(agent);

    mockRequest.mockResolvedValueOnce({ events: [makeEvent("a")] });
    await fetchEvents(agent);

    hung.reject(Object.assign(new Error("{}"), { code: 404, response: { detail: "Agent not found" } }));
    await expect(stale).rejects.toThrow();
    expect(isConversationNotFound(agent)).toBe(false);
    expect(getConversationLoadState(agent)).toEqual({ phase: "idle", error: null });
  });

  it("does not let a superseded attempt's late success replace the newer window", async () => {
    // The fence has to cover the success path too, not just the failure one: a
    // superseded attempt can still succeed, merely late, and the snapshot
    // replaces the window wholesale. Letting the older one land would revert the
    // transcript and strand everything placed since -- and it resets offset and
    // hasMoreAfter with it, so neither backfill nor forward paging could reach
    // the lost events again.
    const agent = freshAgent();
    const hung = deferredResponse();
    mockRequest.mockReturnValueOnce(hung.promise);
    const stale = fetchEvents(agent);

    mockRequest.mockResolvedValueOnce({ events: [makeEvent("a"), makeEvent("b")] });
    await fetchEvents(agent);
    appendEvents(agent, [makeEvent("c")]);
    expect(ids(agent)).toEqual(["a", "b", "c"]);

    hung.resolve({ events: [makeEvent("a")] });
    await stale;
    expect(ids(agent)).toEqual(["a", "b", "c"]);
    expect(hasMoreAfter(agent)).toBe(false);
    expect(getConversationLoadState(agent)).toEqual({ phase: "idle", error: null });
  });

  it("is not moved by a failed backfill page, which leaves the window readable", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("b")], offset: 1, total: 2 });
    await fetchEvents(agent);

    // Paging failures are deliberately non-fatal: the older history just is not
    // loaded. Recording one would blank a transcript the user can still read.
    mockRequest.mockRejectedValueOnce(proxyUnavailableError());
    await fetchBackfillEvents(agent, 50);
    expect(getConversationLoadState(agent)).toEqual({ phase: "idle", error: null });
    expect(ids(agent)).toEqual(["b"]);
  });
});

describe("message-sent listeners", () => {
  it("notifies on a real send, skips whitespace-only, and stops after removal", async () => {
    // sendMessage's request body reads the client identity, which needs
    // localStorage (absent in the node test environment).
    vi.stubGlobal("localStorage", {
      getItem: () => null,
      setItem: () => {},
    });
    try {
      const agent = freshAgent();
      const seen: string[] = [];
      const listener = (agentId: string) => seen.push(agentId);
      addMessageSentListener(listener);
      try {
        mockRequest.mockResolvedValueOnce({});
        await sendMessage(agent, "hello");
        expect(seen).toEqual([agent]);

        // A whitespace-only message returns early: no notification, no request.
        mockRequest.mockClear();
        await sendMessage(agent, "   ");
        expect(seen).toEqual([agent]);
        expect(mockRequest).not.toHaveBeenCalled();
      } finally {
        removeMessageSentListener(listener);
      }

      mockRequest.mockResolvedValueOnce({});
      await sendMessage(agent, "again");
      expect(seen).toEqual([agent]);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("event detail cache", () => {
  const detail = { inputs_by_tool_call_id: { c1: "full input" }, output: "full output", thinking: null };

  it("fetches once and serves later requests from the cache", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce(detail);
    requestEventDetail(agent, "e1");
    expect(getEventDetailState(agent, "e1")).toEqual({ state: "loading" });
    await Promise.resolve();
    expect(getEventDetailState(agent, "e1")).toEqual({ state: "loaded", detail });
    expect(getEventDetailVersion(agent)).toBe(1);

    // Cache hit: a later request (the expanded render's heal pass) fetches nothing.
    requestEventDetail(agent, "e1");
    expect(mockRequest).toHaveBeenCalledTimes(1);
  });

  it("marks a 404 unavailable and never refetches it", async () => {
    const agent = freshAgent();
    mockRequest.mockRejectedValueOnce(Object.assign(new Error("{}"), { code: 404 }));
    requestEventDetail(agent, "gone");
    await Promise.resolve();
    await Promise.resolve();
    expect(getEventDetailState(agent, "gone")).toEqual({ state: "unavailable" });

    requestEventDetail(agent, "gone");
    expect(mockRequest).toHaveBeenCalledTimes(1);
  });

  it("paces the retry of a transient failure instead of looping", async () => {
    // A non-404 failure (backend restarting) must not become a tight fetch loop:
    // the expanded row re-requests on every render, so the failed entry has to keep
    // blocking re-requests until the retry delay elapses.
    vi.useFakeTimers();
    try {
      const agent = freshAgent();
      mockRequest.mockRejectedValueOnce(Object.assign(new Error("boom"), { code: 500 }));
      requestEventDetail(agent, "flaky");
      await Promise.resolve();
      await Promise.resolve();

      // Still "loading": an immediate re-request (a redraw of the expanded row) is a no-op.
      expect(getEventDetailState(agent, "flaky")).toEqual({ state: "loading" });
      requestEventDetail(agent, "flaky");
      expect(mockRequest).toHaveBeenCalledTimes(1);

      // After the delay the entry is dropped, so the next render retries -- once.
      vi.advanceTimersByTime(5000);
      expect(getEventDetailState(agent, "flaky")).toBeUndefined();
      mockRequest.mockResolvedValueOnce(detail);
      requestEventDetail(agent, "flaky");
      await Promise.resolve();
      expect(mockRequest).toHaveBeenCalledTimes(2);
      expect(getEventDetailState(agent, "flaky")).toEqual({ state: "loaded", detail });
    } finally {
      vi.useRealTimers();
    }
  });
});
