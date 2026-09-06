// @vitest-environment jsdom
//
// The provisional-chat state machine, driven through the socket the way the chat app pushes it:
// the messages below are the wire shapes of ``ws_broadcaster.py``.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("mithril", () => ({ default: { redraw: vi.fn(), request: vi.fn() } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({
  apiUrl: (path: string) => path,
  wsUrl: (path: string) => `ws://test${path}`,
}));

import type { AgentState, ProtoAgent } from "./AgentManager";

type AgentManagerModule = typeof import("./AgentManager");

/** Stands in for the page's WebSocket: the manager's handlers are driven by hand and the socket
 *  never closes, so no reconnect timer is ever scheduled. */
class FakeSocket {
  static latest: FakeSocket | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number; reason: string; wasClean: boolean }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public readonly url: string) {
    FakeSocket.latest = this;
  }

  close(): void {}
}

function push(message: object): void {
  const socket = FakeSocket.latest;
  if (socket === null || socket.onmessage === null) throw new Error("the manager opened no socket");
  socket.onmessage({ data: JSON.stringify(message) });
}

/** The socket (re)opening, after which the app replays what it holds. */
function open(): void {
  const socket = FakeSocket.latest;
  if (socket === null || socket.onopen === null) throw new Error("the manager opened no socket");
  socket.onopen();
}

function agent(id: string): AgentState {
  return { id, name: id, state: "RUNNING", labels: {}, work_dir: null };
}

function proto(agentId: string, phase: ProtoAgent["phase"], error: string | null = null): ProtoAgent {
  return { agent_id: agentId, name: "Chat 1", account_id: "acct-1", phase, error };
}

/** Whether a promise has settled yet, without waiting on it: the hold must be observable. A
 *  settled promise's reaction runs before the timer's macrotask, so the race is decisive. */
function settledState(promise: Promise<void>): Promise<"pending" | "resolved" | "rejected"> {
  return Promise.race([
    promise.then(
      () => "resolved" as const,
      () => "rejected" as const,
    ),
    new Promise<"pending">((resolve) => setTimeout(() => resolve("pending"), 0)),
  ]);
}

describe("the provisional chats over the socket", () => {
  let manager: AgentManagerModule;

  beforeEach(async () => {
    vi.resetModules();
    vi.stubGlobal("WebSocket", FakeSocket);
    manager = await import("./AgentManager");
    manager.initAgentManager();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    FakeSocket.latest = null;
  });

  it("stores a pushed record and replaces it when the same chat is pushed in a new phase", () => {
    push({ type: "proto_agent_created", ...proto("agent-1", "awaiting_account") });
    expect(manager.getProtoAgent("agent-1")?.phase).toBe("awaiting_account");

    push({ type: "proto_agent_created", ...proto("agent-1", "creating") });
    expect(manager.getProtoAgent("agent-1")?.phase).toBe("creating");
  });

  it("drops the record and releases a held send once the agent list names the chat", async () => {
    push({ type: "proto_agent_created", ...proto("agent-1", "creating") });
    const registered = manager.whenAgentRegistered("agent-1");
    expect(await settledState(registered)).toBe("pending");

    push({ type: "agents_updated", agents: [agent("agent-1")] });

    await expect(registered).resolves.toBeUndefined();
    expect(manager.getProtoAgent("agent-1")).toBeUndefined();
    expect(manager.getAgentById("agent-1")?.id).toBe("agent-1");
  });

  it("resolves at once for a chat the app already lists", async () => {
    push({ type: "agents_updated", agents: [agent("agent-1")] });
    await expect(manager.whenAgentRegistered("agent-1")).resolves.toBeUndefined();
  });

  it("resolves at once for a chat the app neither lists nor is creating, so the send reports the refusal", async () => {
    push({ type: "agents_updated", agents: [] });
    await expect(manager.whenAgentRegistered("agent-gone")).resolves.toBeUndefined();
  });

  it("marks a failed create on its record and rejects a held send with the reason", async () => {
    push({ type: "proto_agent_created", ...proto("agent-1", "creating") });
    const registered = manager.whenAgentRegistered("agent-1");

    push({
      type: "proto_agent_completed",
      agent_id: "agent-1",
      success: false,
      error: "mngr create exited with code 3",
    });

    await expect(registered).rejects.toThrow("mngr create exited with code 3");
    expect(manager.getProtoAgent("agent-1")).toMatchObject({
      phase: "failed",
      error: "mngr create exited with code 3",
    });
  });

  it("keeps a held send waiting after a successful completion until the agent list names the chat", async () => {
    push({ type: "proto_agent_created", ...proto("agent-1", "creating") });
    const registered = manager.whenAgentRegistered("agent-1");

    push({ type: "proto_agent_completed", agent_id: "agent-1", success: true, error: null });

    // The record is gone, but the send has nothing to reach until the list carries the agent.
    expect(manager.getProtoAgent("agent-1")).toBeUndefined();
    expect(await settledState(registered)).toBe("pending");
    push({ type: "agents_updated", agents: [agent("agent-1")] });
    await expect(registered).resolves.toBeUndefined();
  });

  it("lets the agent list win over a record replayed after the chat registered", async () => {
    push({ type: "agents_updated", agents: [agent("agent-1")] });
    push({ type: "proto_agent_created", ...proto("agent-1", "creating") });

    // A late record does not unlist the agent, and a send reaches it at once: the record is
    // what the page's provisionalRecord discards while the list names the chat.
    expect(manager.getAgentById("agent-1")?.id).toBe("agent-1");
    await expect(manager.whenAgentRegistered("agent-1")).resolves.toBeUndefined();
  });

  it("drops a discarded chat and rejects a held send", async () => {
    push({ type: "proto_agent_created", ...proto("agent-1", "creating") });
    const registered = manager.whenAgentRegistered("agent-1");

    push({ type: "proto_agent_completed", agent_id: "agent-1", success: false, error: null });

    await expect(registered).rejects.toThrow("closed before it started");
    expect(manager.getProtoAgent("agent-1")).toBeUndefined();
  });

  it("drops a record a reconnect's replay does not carry and releases the send held for it", async () => {
    push({ type: "agents_updated", agents: [] });
    push({ type: "proto_agent_created", ...proto("agent-1", "creating") });
    push({ type: "proto_agent_created", ...proto("agent-2", "creating") });
    const registered = manager.whenAgentRegistered("agent-1");

    // The chat app restarted while agent-1's create ran: the new process replays only the
    // record it holds, then an agent list identical to the last one.
    open();
    push({ type: "proto_agent_created", ...proto("agent-2", "creating") });
    // Nothing is dropped until the list says the replay is over.
    expect(manager.getProtoAgent("agent-1")?.phase).toBe("creating");
    expect(await settledState(registered)).toBe("pending");
    push({ type: "agents_updated", agents: [] });

    expect(manager.getProtoAgent("agent-1")).toBeUndefined();
    expect(manager.getProtoAgent("agent-2")?.phase).toBe("creating");
    await expect(registered).resolves.toBeUndefined();
  });

  it("keeps a record a reconnect's replay carries, and the send held for it", async () => {
    push({ type: "proto_agent_created", ...proto("agent-1", "creating") });
    const registered = manager.whenAgentRegistered("agent-1");

    open();
    push({ type: "proto_agent_created", ...proto("agent-1", "creating") });
    push({ type: "agents_updated", agents: [] });

    expect(manager.getProtoAgent("agent-1")?.phase).toBe("creating");
    expect(await settledState(registered)).toBe("pending");
    push({ type: "agents_updated", agents: [agent("agent-1")] });
    await expect(registered).resolves.toBeUndefined();
  });

  it("rejects a held send when a reconnect replays the chat as failed", async () => {
    push({ type: "proto_agent_created", ...proto("agent-1", "creating") });
    const registered = manager.whenAgentRegistered("agent-1");

    push({ type: "proto_agent_created", ...proto("agent-1", "failed", "the real reason") });

    await expect(registered).rejects.toThrow("the real reason");
    expect(manager.getProtoAgent("agent-1")?.phase).toBe("failed");
  });
});
