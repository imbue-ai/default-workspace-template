// @vitest-environment jsdom
//
// When a streamed chat's page asks the transcript route for what its stream missed: the turn's
// end, as the chat list's pushes report it.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { mockRequest } = vi.hoisted(() => ({ mockRequest: vi.fn() }));
vi.mock("mithril", () => ({ default: { redraw: vi.fn(), request: mockRequest } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({
  apiUrl: (path: string) => path,
  wsUrl: (path: string) => `ws://test${path}`,
}));

import { chatSnapshotFixture } from "./chatSnapshotFixture";
import type { TranscriptEvent } from "./Response";

class FakeSocket {
  static latest: FakeSocket | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public readonly url: string) {
    FakeSocket.latest = this;
  }

  close(): void {}
}

class FakeEventSource {
  onmessage: ((event: { data: string }) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public readonly url: string) {}

  close(): void {}
}

function pushChats(chats: object[]): void {
  FakeSocket.latest?.onmessage?.({ data: JSON.stringify({ type: "chats_updated", chats }) });
}

function userTurn(id: string): TranscriptEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "user_message",
    event_id: id,
    source: "test",
    role: "user",
    content: id,
  };
}

/** The `after` cursors of the catch-up requests made so far. */
function catchUpCursors(): string[] {
  return mockRequest.mock.calls.flatMap(([options]) => {
    const after = (options as { params?: { after?: string } }).params?.after;
    return after === undefined ? [] : [after];
  });
}

const CHAT_ID = "agent-catchup";

describe("the catch-up triggers", () => {
  beforeEach(async () => {
    vi.resetModules();
    mockRequest.mockReset();
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("EventSource", FakeEventSource);
    (await import("./Chats")).initChats();
    mockRequest.mockResolvedValueOnce({ events: [userTurn("turn-1")], offset: 0, total: 1 });
    await (await import("./StreamingMessage")).loadSnapshotWithStream(CHAT_ID);
    mockRequest.mockResolvedValue({ events: [], offset: 1, total: 1 });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    FakeSocket.latest = null;
  });

  it("asks for what follows the last held event when the turn's activity reaches IDLE", () => {
    pushChats([chatSnapshotFixture(CHAT_ID, { status: "working", active_agent: { activity_state: "THINKING" } })]);
    pushChats([chatSnapshotFixture(CHAT_ID, { status: "working", active_agent: { activity_state: "IDLE" } })]);

    expect(catchUpCursors()).toEqual(["turn-1"]);
  });

  it("asks when the chat stops working although its activity never left IDLE", () => {
    pushChats([chatSnapshotFixture(CHAT_ID, { status: "working", active_agent: { activity_state: "IDLE" } })]);
    pushChats([chatSnapshotFixture(CHAT_ID, { status: "stopped", active_agent: { activity_state: "IDLE" } })]);

    expect(catchUpCursors()).toEqual(["turn-1"]);
  });

  it("asks nothing while the turn is still running", () => {
    pushChats([chatSnapshotFixture(CHAT_ID, { status: "idle", active_agent: { activity_state: "IDLE" } })]);
    pushChats([chatSnapshotFixture(CHAT_ID, { status: "working", active_agent: { activity_state: "THINKING" } })]);

    expect(catchUpCursors()).toEqual([]);
  });
});

class OpenableEventSource extends FakeEventSource {
  static latest: OpenableEventSource | null = null;

  constructor(url: string) {
    super(url);
    OpenableEventSource.latest = this;
  }
}

// A snapshot can be read before the server registers the page's stream (the chat root reads it as it creates the
// frame), so whatever the two land in, the page asks once both have.
describe("the catch-up once the snapshot and the open stream have both landed", () => {
  beforeEach(async () => {
    vi.resetModules();
    mockRequest.mockReset();
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("EventSource", OpenableEventSource);
    (await import("./Chats")).initChats();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    FakeSocket.latest = null;
    OpenableEventSource.latest = null;
  });

  it("asks when the stream opens after the snapshot landed", async () => {
    mockRequest.mockResolvedValueOnce({ events: [userTurn("turn-1")], offset: 0, total: 1 });
    await (await import("./StreamingMessage")).loadSnapshotWithStream(CHAT_ID);
    mockRequest.mockResolvedValue({ events: [], offset: 1, total: 1 });
    expect(catchUpCursors()).toEqual([]);

    OpenableEventSource.latest?.onopen?.();

    expect(catchUpCursors()).toEqual(["turn-1"]);
  });

  it("asks when the snapshot lands after the stream opened, and not before", async () => {
    let landSnapshot: (value: unknown) => void = () => undefined;
    mockRequest.mockReturnValueOnce(new Promise((resolve) => (landSnapshot = resolve)));
    const loaded = (await import("./StreamingMessage")).loadSnapshotWithStream(CHAT_ID);
    OpenableEventSource.latest?.onopen?.();
    expect(catchUpCursors()).toEqual([]);

    mockRequest.mockResolvedValue({ events: [], offset: 1, total: 1 });
    landSnapshot({ events: [userTurn("turn-1")], offset: 0, total: 1 });
    await loaded;

    expect(catchUpCursors()).toEqual(["turn-1"]);
  });
});
