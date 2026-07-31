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

import { disconnectFromStream, loadSnapshotWithStream, SSE_SILENCE_TIMEOUT_MS } from "./StreamingMessage";
import { getEventsForAgent, type TranscriptEvent } from "./Response";

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
  readonly listeners = new Map<string, Array<() => void>>();
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: () => void): void {
    const existing = this.listeners.get(type) ?? [];
    existing.push(listener);
    this.listeners.set(type, existing);
  }
  /** Deliver a named (non-`message`) server event, e.g. the keepalive ping. */
  emit(type: string): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener();
    }
  }
  close(): void {
    this.closed = true;
  }
}

function latestEventSource(): FakeEventSource {
  const instance = FakeEventSource.instances[FakeEventSource.instances.length - 1];
  expect(instance).toBeDefined();
  return instance;
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
  vi.useRealTimers();
});

describe("loadSnapshotWithStream", () => {
  it("does not drop an SSE delta that races the initial snapshot fetch", async () => {
    const agentId = `agent-${agentCounter++}`;
    const snapshotEvent = makeEvent("snap-1", "from snapshot");
    const delta = makeEvent("delta-1", "live delta during fetch");

    // Leave the snapshot fetch pending so we can interleave a live delta.
    const snapshotRequest = deferred<{ events: TranscriptEvent[] }>();
    mockRequest.mockReturnValue(snapshotRequest.promise);

    const loadPromise = loadSnapshotWithStream(agentId);

    // The stream is open and the snapshot is still in flight: a live event
    // arrives now. Without buffering, the snapshot replace below would drop it.
    const eventSource = latestEventSource();
    eventSource.onmessage?.({ data: JSON.stringify(delta) });

    snapshotRequest.resolve({ events: [snapshotEvent] });
    await loadPromise;

    const ids = getEventsForAgent(agentId).map((event) => event.event_id);
    expect(ids).toContain("snap-1");
    expect(ids).toContain("delta-1");

    // Streams (and the shared liveness timer behind them) are module state, so
    // leaving one connected would leak into the next test.
    disconnectFromStream(agentId);
  });
});

// A half-dead SSE connection delivers no error and no close: the transcript just
// stops while the terminal (whose WebSocket has its own keepalive) keeps looking
// healthy. The only evidence available to the client is silence, so the watchdog
// is what turns "nothing has arrived in a while" into a reconnect.
describe("SSE liveness watchdog", () => {
  async function connect(agentId: string): Promise<void> {
    mockRequest.mockResolvedValue({ events: [], offset: 0, total: 0 });
    await loadSnapshotWithStream(agentId);
  }

  it("reconnects a stream that has gone silent", async () => {
    vi.useFakeTimers();
    const agentId = `agent-${agentCounter++}`;
    await connect(agentId);
    const original = latestEventSource();
    expect(FakeEventSource.instances).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(SSE_SILENCE_TIMEOUT_MS + 5_000);

    expect(original.closed).toBe(true);
    // Recovery goes through the snapshot path, so a fresh stream exists and the
    // transcript was re-read whole.
    expect(FakeEventSource.instances.length).toBeGreaterThan(1);
    expect(latestEventSource().closed).toBe(false);

    disconnectFromStream(agentId);
  });

  it("leaves a stream alone while the server keeps pinging", async () => {
    vi.useFakeTimers();
    const agentId = `agent-${agentCounter++}`;
    await connect(agentId);
    const original = latestEventSource();

    // Server keepalives keep arriving on an otherwise silent stream. They are
    // named events, invisible to onmessage, which is exactly why the server
    // emits them as events rather than as SSE comments.
    for (let elapsed = 0; elapsed < SSE_SILENCE_TIMEOUT_MS * 3; elapsed += 8_000) {
      await vi.advanceTimersByTimeAsync(8_000);
      original.emit("ping");
    }

    expect(original.closed).toBe(false);
    expect(FakeEventSource.instances).toHaveLength(1);

    disconnectFromStream(agentId);
  });

  it("stops watching once every stream is disconnected", async () => {
    vi.useFakeTimers();
    const agentId = `agent-${agentCounter++}`;
    await connect(agentId);
    disconnectFromStream(agentId);
    expect(vi.getTimerCount()).toBe(0);

    // ...and nothing resurrects the stream afterwards.
    await vi.advanceTimersByTimeAsync(SSE_SILENCE_TIMEOUT_MS + 5_000);
    expect(FakeEventSource.instances).toHaveLength(1);
  });
});
