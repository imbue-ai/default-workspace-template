import { afterEach, describe, expect, it, vi } from "vitest";

// The wake-reconnect tests below drive socket handlers that call m.redraw();
// mock mithril so no real render machinery (requestAnimationFrame, DOM) is
// needed. buildSessionTerminalUrl does not touch mithril and is unaffected.
const { mockRedraw } = vi.hoisted(() => ({ mockRedraw: vi.fn() }));
vi.mock("mithril", () => ({
  default: { redraw: mockRedraw, request: vi.fn() },
}));

import { buildSessionTerminalUrl } from "./AgentManager";

/** Read back the repeated ``arg`` query params in order. */
function parseArgs(url: string): string[] {
  const query = url.split("?")[1] ?? "";
  return new URLSearchParams(query).getAll("arg");
}

describe("buildSessionTerminalUrl", () => {
  it("emits the positional args in ttyd dispatch order", () => {
    const url = buildSessionTerminalUrl("terminal-1", "term-abc", "/mngr/code");
    expect(url.startsWith("/service/terminal/?")).toBe(true);
    expect(parseArgs(url)).toEqual(["_", "session", "terminal-1", "term-abc", "/mngr/code"]);
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

// A fake WebSocket that records construction and, like a real browser, fires
// ``onclose`` from ``close()`` -- so the tests prove the wake path detaches the
// dead socket's handlers before closing it.
class FakeWebSocket {
  static readonly instances: FakeWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  close(): void {
    this.closed = true;
    this.onclose?.();
  }
}

// AgentManager holds module-level connection state (the ws singleton, the
// wake-coalescing flag), so each test stubs the globals and then imports a fresh
// copy of the module before wiring it up.
async function freshAgentManager(): Promise<{
  am: typeof import("./AgentManager");
  fire: (type: string) => void;
}> {
  const listeners = new Map<string, (() => void)[]>();
  const capture = (type: string, cb: () => void): void => {
    listeners.set(type, [...(listeners.get(type) ?? []), cb]);
  };
  const fire = (type: string): void => {
    for (const cb of listeners.get(type) ?? []) {
      cb();
    }
  };
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal("document", { addEventListener: capture, querySelector: () => null, visibilityState: "visible" });
  vi.stubGlobal("window", { addEventListener: capture, location: { protocol: "http:", host: "localhost:8000" } });
  vi.resetModules();
  const am = await import("./AgentManager");
  am.initAgentManager();
  return { am, fire };
}

/** A populated agents_updated snapshot, one agent per given activity_state. */
function agentsUpdatedMessage(activityByAgentId: Record<string, string | null>): { data: string } {
  const agentStates = Object.entries(activityByAgentId).map(([id, activity_state]) => ({
    id,
    name: id,
    state: "RUNNING",
    labels: {},
    work_dir: `/w/${id}`,
    activity_state,
  }));
  return { data: JSON.stringify({ type: "agents_updated", agents: agentStates }) };
}

describe("wake reconnect", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    FakeWebSocket.instances.length = 0;
  });

  it("tears down the stale socket and opens a fresh one when the machine wakes", async () => {
    const { fire } = await freshAgentManager();
    expect(FakeWebSocket.instances).toHaveLength(1);
    const stale = FakeWebSocket.instances[0];

    // The machine wakes: the browser never fired onclose on the dead socket, so
    // becoming visible is what has to trigger the reconnect.
    fire("visibilitychange");

    // The stale socket was closed and a brand-new one opened.
    expect(stale.closed).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(2);
    // Its handlers were detached first, so the late onclose from close() could
    // not run against (and null out) the freshly opened replacement.
    expect(stale.onclose).toBeNull();
    expect(FakeWebSocket.instances[1].onclose).not.toBeNull();

    // The burst of focus + online that a single wake also fires is coalesced
    // into the one reconnect above, not one reconnect per event.
    fire("focus");
    fire("online");
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it("drops stale proto agents when the replacement connection opens", async () => {
    const { am, fire } = await freshAgentManager();
    const first = FakeWebSocket.instances[0];
    first.onopen?.();
    first.onmessage?.({
      data: JSON.stringify({
        type: "proto_agent_created",
        agent_id: "proto-1",
        name: "building",
        creation_type: "chat",
        parent_agent_id: null,
      }),
    });
    expect(am.getProtoAgents()).toHaveLength(1);

    // Sleep kills the connection silently; while asleep the proto agent
    // completes, but proto_agent_completed is never replayed on reconnect.
    fire("visibilitychange");
    const replacement = FakeWebSocket.instances[1];
    replacement.onopen?.();

    // The fresh snapshot's replayed proto_agent_created events rebuild the
    // set; a proto that completed while disconnected must not linger.
    expect(am.getProtoAgents()).toHaveLength(0);
  });
});

describe("connection-state listeners", () => {
  afterEach(() => {
    // onclose schedules a real reconnect timer; fake timers (enabled per test)
    // keep it from firing after the test and opening a live socket.
    vi.useRealTimers();
    vi.unstubAllGlobals();
    FakeWebSocket.instances.length = 0;
  });

  it("fires the listener with true on open and false on close", async () => {
    const { am } = await freshAgentManager();
    vi.useFakeTimers();
    const states: boolean[] = [];
    am.addConnectionStateListener((connected) => states.push(connected));

    const ws = FakeWebSocket.instances[0];
    expect(am.isConnected()).toBe(false);

    ws.onopen?.();
    expect(am.isConnected()).toBe(true);

    ws.onclose?.();
    expect(am.isConnected()).toBe(false);

    expect(states).toEqual([true, false]);
  });

  it("stops firing after the listener unsubscribes, but still tracks state internally", async () => {
    const { am } = await freshAgentManager();
    vi.useFakeTimers();
    const states: boolean[] = [];
    const listener = (connected: boolean): void => {
      states.push(connected);
    };
    am.addConnectionStateListener(listener);

    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();
    expect(states).toEqual([true]);

    am.removeConnectionStateListener(listener);
    ws.onclose?.();

    // The unsubscribed listener heard nothing more, though isConnected still
    // reflects the close.
    expect(states).toEqual([true]);
    expect(am.isConnected()).toBe(false);
  });
});

describe("activity_state on disconnect", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    FakeWebSocket.instances.length = 0;
  });

  it("nulls every agent's activity_state on close and broadcasts the change", async () => {
    const { am } = await freshAgentManager();
    vi.useFakeTimers();
    const broadcasts: (string | null)[][] = [];
    am.addAgentsUpdatedListener((updated) => {
      broadcasts.push(updated.map((a) => a.activity_state ?? null));
    });

    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();
    ws.onmessage?.(agentsUpdatedMessage({ a1: "THINKING", a2: "TOOL_RUNNING" }));
    expect(am.getAgentById("a1")?.activity_state).toBe("THINKING");
    expect(am.getAgentById("a2")?.activity_state).toBe("TOOL_RUNNING");

    ws.onclose?.();

    // activity_state is now unknown for every agent...
    expect(am.getAgentById("a1")?.activity_state).toBeNull();
    expect(am.getAgentById("a2")?.activity_state).toBeNull();
    // ...but the rest of each agent's metadata is untouched...
    expect(am.getAgentById("a1")?.name).toBe("a1");
    expect(am.getAgentById("a1")?.work_dir).toBe("/w/a1");
    // ...and the nulling was broadcast to agents_updated listeners so views redraw.
    expect(broadcasts[broadcasts.length - 1]).toEqual([null, null]);
  });

  it("repopulates real activity_state from the snapshot after reconnect", async () => {
    const { am, fire } = await freshAgentManager();
    vi.useFakeTimers();
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();
    ws.onmessage?.(agentsUpdatedMessage({ a1: "THINKING", a2: "TOOL_RUNNING" }));

    ws.onclose?.();
    expect(am.getAgentById("a1")?.activity_state).toBeNull();
    expect(am.getAgentById("a2")?.activity_state).toBeNull();

    // A wake opens a fresh socket synchronously (ws is null after close), whose
    // replayed snapshot carries the true current states.
    fire("visibilitychange");
    const reconnected = FakeWebSocket.instances[1];
    reconnected.onopen?.();
    reconnected.onmessage?.(agentsUpdatedMessage({ a1: "IDLE", a2: "THINKING" }));

    expect(am.getAgentById("a1")?.activity_state).toBe("IDLE");
    expect(am.getAgentById("a2")?.activity_state).toBe("THINKING");
  });
});
