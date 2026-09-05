import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Capture mithril's request/redraw via hoisted mocks so the test can control
// when the snapshot fetch resolves without fighting mithril's overloaded types.
const { mockRequest, mockRedraw } = vi.hoisted(() => ({
  mockRequest: vi.fn(),
  mockRedraw: vi.fn(),
}));
vi.mock("mithril", () => ({
  default: { request: mockRequest, redraw: mockRedraw },
}));

import { loadSnapshotWithStream } from "./StreamingMessage";
import { asChatId } from "../ids";
import { getConversationLoadState, getEventsForAgent, type TranscriptEvent } from "./Response";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onmessage: ((event: { data: string }) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
  close(): void {
    this.closed = true;
  }
}

function makeEvent(id: string, content: string): TranscriptEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "user_message",
    event_id: id,
    source: "test",
    message_uuid: id,
    role: "user",
    content,
  };
}

let agentCounter = 0;

beforeEach(() => {
  FakeEventSource.instances = [];
  globalThis.EventSource = FakeEventSource as unknown as typeof EventSource;
  globalThis.document = { querySelector: () => null } as unknown as Document;
  mockRequest.mockReset();
  mockRedraw.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("loadSnapshotWithStream", () => {
  it("does not drop an SSE delta that races the initial snapshot fetch", async () => {
    const agentId = asChatId(`agent-${agentCounter++}`);
    const snapshotEvent = makeEvent("snap-1", "from snapshot");
    const delta = makeEvent("delta-1", "live delta during fetch");

    // Leave the snapshot fetch pending so we can interleave a live delta.
    const snapshotRequest = deferred<{ events: TranscriptEvent[] }>();
    mockRequest.mockReturnValue(snapshotRequest.promise);

    const loadPromise = loadSnapshotWithStream(agentId);

    // The stream is open and the snapshot is still in flight: a live event
    // arrives now. Without buffering, the snapshot replace below would drop it.
    const eventSource = FakeEventSource.instances[FakeEventSource.instances.length - 1];
    expect(eventSource).toBeDefined();
    eventSource?.onmessage?.({ data: JSON.stringify(delta) });

    snapshotRequest.resolve({ events: [snapshotEvent] });
    await loadPromise;

    const ids = getEventsForAgent(agentId).map((event) => event.event_id);
    expect(ids).toContain("snap-1");
    expect(ids).toContain("delta-1");
  });
});

describe("snapshot retry after reconnect", () => {
  it("keeps retrying a failed snapshot refetch until it succeeds", async () => {
    // A single failed snapshot used to be terminal: the stream kept appending
    // deltas onto the pre-outage window, so events emitted during the outage
    // were silently missing forever (transcript desynchronized from the TUI).
    vi.useFakeTimers();
    try {
      const agentId = asChatId(`agent-${agentCounter++}`);
      mockRequest.mockResolvedValueOnce({ events: [makeEvent("initial", "before outage")] });
      await loadSnapshotWithStream(agentId);

      // The stream dies; the error-path reconnect fires after its backoff, and
      // its snapshot refetch fails (the backend is still unreachable).
      mockRequest.mockRejectedValueOnce(new Error("503 through reconnecting tunnel"));
      const deadSource = FakeEventSource.instances[FakeEventSource.instances.length - 1];
      deadSource?.onerror?.();
      await vi.advanceTimersByTimeAsync(2000);

      // The retry must refetch on its own (no further stream error to prompt
      // it) and land the authoritative window including the outage events.
      mockRequest.mockResolvedValueOnce({
        events: [makeEvent("initial", "before outage"), makeEvent("missed", "emitted during outage")],
      });
      await vi.advanceTimersByTimeAsync(6000);

      const ids = getEventsForAgent(agentId).map((event) => event.event_id);
      expect(ids).toContain("missed");
    } finally {
      vi.useRealTimers();
    }
  });

  it("clears the recorded load error once the retry lands", async () => {
    // The panel renders that error, and this loop is the only thing still
    // trying: a chat whose first load 503'd through a reconnecting tunnel used
    // to sit on the error screen with a fully re-synced transcript behind it,
    // recoverable only by reloading the page.
    vi.useFakeTimers();
    try {
      const agentId = asChatId(`agent-${agentCounter++}`);
      mockRequest.mockRejectedValueOnce(Object.assign(new Error(String(null)), { code: 503, response: null }));
      await expect(loadSnapshotWithStream(agentId)).rejects.toThrow();
      expect(getConversationLoadState(agentId).error).toBe("request failed (HTTP 503)");

      mockRequest.mockResolvedValueOnce({ events: [makeEvent("after-recovery", "backend answered")] });
      const deadSource = FakeEventSource.instances[FakeEventSource.instances.length - 1];
      deadSource?.onerror?.();
      await vi.advanceTimersByTimeAsync(6000);

      expect(getConversationLoadState(agentId)).toEqual({ phase: "idle", error: null });
      expect(getEventsForAgent(agentId).map((event) => event.event_id)).toContain("after-recovery");
    } finally {
      vi.useRealTimers();
    }
  });
});
