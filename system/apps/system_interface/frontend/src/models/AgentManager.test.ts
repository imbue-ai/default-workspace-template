import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("mithril", () => ({ default: { request: vi.fn(), redraw: vi.fn() } }));

import {
  addAgentRemovedListener,
  buildSessionTerminalUrl,
  initAgentManager,
  removeAgentRemovedListener,
} from "./AgentManager";

/** Read back the repeated ``arg`` query params in order. */
function parseArgs(url: string): string[] {
  const query = url.split("?")[1] ?? "";
  return new URLSearchParams(query).getAll("arg");
}

describe("buildSessionTerminalUrl", () => {
  it("emits the positional args in ttyd dispatch order", () => {
    const url = buildSessionTerminalUrl("terminal-1", "term-abc", "/home/user/workspace");
    expect(url.startsWith("/service/terminal/?")).toBe(true);
    expect(parseArgs(url)).toEqual(["_", "session", "terminal-1", "term-abc", "/home/user/workspace"]);
  });

  it("omits the working directory arg as empty when none is given", () => {
    const url = buildSessionTerminalUrl("terminal-2", "term-xyz", "");
    expect(parseArgs(url)).toEqual(["_", "session", "terminal-2", "term-xyz", ""]);
  });

  it("percent-encodes special characters but round-trips the original values", () => {
    const url = buildSessionTerminalUrl("my term", "id", "/a b/c");
    // The raw query must not carry literal spaces...
    expect(url).not.toContain(" ");
    // ...but decoding recovers the exact session name and workdir.
    expect(parseArgs(url)).toEqual(["_", "session", "my term", "id", "/a b/c"]);
  });
});

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static readonly OPEN = 1;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 1;
  sent: string[] = [];
  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {}
}

// The destroyed-agent signal crosses a wire, so the field names have to agree
// with what the backend broadcasts. Both sides are tested separately and would
// both keep passing if the names silently drifted apart, so this pins the shape
// the socket actually delivers.
describe("agent_removed", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    globalThis.document = { querySelector: () => null } as unknown as Document;
    globalThis.window = { location: { protocol: "http:", host: "localhost:8000" } } as unknown as Window &
      typeof globalThis;
    initAgentManager();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function deliver(message: unknown): void {
    const socket = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
    socket?.onmessage?.({ data: JSON.stringify(message) });
  }

  it("delivers the destroyed agent's id and name to listeners", () => {
    const removed: Array<[string, string]> = [];
    const listener = (agentId: string, agentName: string): void => {
      removed.push([agentId, agentName]);
    };
    addAgentRemovedListener(listener);

    deliver({ type: "agent_removed", agent_id: "agent-123", agent_name: "migrate-workspace" });

    expect(removed).toEqual([["agent-123", "migrate-workspace"]]);
    removeAgentRemovedListener(listener);
  });

  it("does not fire for an agent merely absent from an agents_updated snapshot", () => {
    const removed: string[] = [];
    const listener = (agentId: string): void => {
      removed.push(agentId);
    };
    addAgentRemovedListener(listener);

    // The agent this client knew about is simply not in the new list. That is
    // what a restarting discovery pipeline looks like, and it must not be
    // reported as a destroy.
    deliver({ type: "agents_updated", agents: [] });

    expect(removed).toEqual([]);
    removeAgentRemovedListener(listener);
  });

  it("stops notifying a removed listener", () => {
    const removed: string[] = [];
    const listener = (agentId: string): void => {
      removed.push(agentId);
    };
    addAgentRemovedListener(listener);
    removeAgentRemovedListener(listener);

    deliver({ type: "agent_removed", agent_id: "agent-123", agent_name: "gone" });

    expect(removed).toEqual([]);
  });
});
