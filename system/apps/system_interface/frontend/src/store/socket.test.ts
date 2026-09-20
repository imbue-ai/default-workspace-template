// @vitest-environment jsdom
/**
 * The shell socket over a stub WebSocket: what reaches the handlers from each message type (and
 * what does not: another client's op, an unknown op, the tabbed shell's messages, a malformed
 * frame), the client-state report, and the reconnect after a close.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ShellSocket } from "./socket";
import type { SocketHandlers } from "./socket";

class StubWebSocket {
  static readonly OPEN = 1;
  static readonly instances: StubWebSocket[] = [];
  readyState = 0;
  readonly sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    StubWebSocket.instances.push(this);
  }

  open(): void {
    this.readyState = StubWebSocket.OPEN;
    this.onopen?.();
  }

  receive(data: unknown): void {
    this.onmessage?.({ data: typeof data === "string" ? data : JSON.stringify(data) } as MessageEvent);
  }

  close(): void {
    this.readyState = 3;
    this.onclose?.({ code: 1006, reason: "", wasClean: false } as CloseEvent);
  }

  send(data: string): void {
    this.sent.push(data);
  }
}

let handlers: SocketHandlers;
let socket: ShellSocket;

function current(): StubWebSocket {
  const instance = StubWebSocket.instances[StubWebSocket.instances.length - 1];
  if (instance === undefined) throw new Error("no socket opened");
  return instance;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(console, "info").mockImplementation(() => undefined);
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  StubWebSocket.instances.length = 0;
  vi.stubGlobal("WebSocket", StubWebSocket);
  handlers = {
    onAppsUpdated: vi.fn(),
    onDesktopsUpdated: vi.fn(),
    onPlacementsUpdated: vi.fn(),
    onActiveDesktopChanged: vi.fn(),
    onLayoutOp: vi.fn(),
    onConnected: vi.fn(),
  };
  socket = new ShellSocket("client-1");
  socket.connect(handlers);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("ShellSocket", () => {
  it("opens the shell's socket, says so once open, and reports the client state only while open", () => {
    expect(current().url).toMatch(/^ws:\/\/.+\/api\/ws$/);
    socket.reportClientState("home", "");
    expect(current().sent).toEqual([]);
    current().open();
    expect(handlers.onConnected).toHaveBeenCalledTimes(1);
    socket.reportClientState("home", "work");
    expect(current().sent.map((raw) => JSON.parse(raw) as unknown)).toEqual([
      { type: "client_state", client_id: "client-1", active_desktop: "home", previous_desktop: "work" },
    ]);
  });

  it("hands each desktop message to its handler with the fields coerced", () => {
    current().open();
    current().receive({ type: "apps_updated", apps: [] });
    current().receive({ type: "desktops_updated", desktops: [] });
    current().receive({ type: "placements_updated", desktop_id: "home", client_id: "client-1", save_id: "s-1" });
    current().receive({ type: "active_desktop_changed", client_id: "client-1" });
    expect(handlers.onAppsUpdated).toHaveBeenCalledWith([]);
    expect(handlers.onDesktopsUpdated).toHaveBeenCalledWith([]);
    expect(handlers.onPlacementsUpdated).toHaveBeenCalledWith({
      desktopId: "home",
      clientId: "client-1",
      saveId: "s-1",
    });
    expect(handlers.onActiveDesktopChanged).toHaveBeenCalledWith({ clientId: "client-1", desktopId: "" });
  });

  it("delivers a layout op for this client or for everyone, and drops another client's or an unknown op", () => {
    current().open();
    current().receive({ type: "layout_op", op: "refresh", args: { window: "win-1" }, target_client_id: "client-1" });
    current().receive({ type: "layout_op", op: "reload_system_interface", args: {}, requester: "chat:agent-1" });
    current().receive({ type: "layout_op", op: "refresh", args: { window: "win-2" }, target_client_id: "client-2" });
    current().receive({ type: "layout_op", op: "focus", args: { window: "win-1" } });
    current().receive({ type: "layout_op", op: "refresh", args: null });
    expect(vi.mocked(handlers.onLayoutOp).mock.calls.map((call) => call[0])).toEqual([
      { op: "refresh", args: { window: "win-1" }, requester: "" },
      { op: "reload_system_interface", args: {}, requester: "chat:agent-1" },
      { op: "refresh", args: {}, requester: "" },
    ]);
  });

  it("ignores the tabbed shell's messages and a malformed frame", () => {
    current().open();
    current().receive({ type: "layout_updated", view: "everything" });
    current().receive({ type: "projects_updated", projects: [] });
    current().receive("{not json");
    for (const handler of Object.values(handlers))
      expect(handler).toHaveBeenCalledTimes(handler === handlers.onConnected ? 1 : 0);
  });

  it("reconnects after a close, on a fresh socket, and says so again once it opens", () => {
    current().open();
    const first = current();
    first.close();
    expect(StubWebSocket.instances).toHaveLength(1);
    vi.runOnlyPendingTimers();
    expect(StubWebSocket.instances).toHaveLength(2);
    expect(current()).not.toBe(first);
    current().open();
    expect(handlers.onConnected).toHaveBeenCalledTimes(2);
  });
});
