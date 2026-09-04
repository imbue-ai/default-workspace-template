// @vitest-environment jsdom
/**
 * The chat page's presence reports: what the chat app's presence route is told, and when
 * (the initial state, every change, a heartbeat of the current state, nothing after closed).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../base-path", () => ({
  apiUrl: (path: string) => path,
}));

interface PresenceRequest {
  url: string;
  body: { client_id: string; state: string };
  keepalive: boolean | undefined;
}

const fetchSpy = vi.fn(() => Promise.resolve(new Response()));

function requests(): PresenceRequest[] {
  return (fetchSpy.mock.calls as unknown as Array<[string, RequestInit]>).map(([url, init]) => ({
    url,
    body: JSON.parse(init.body as string) as { client_id: string; state: string },
    keepalive: init.keepalive,
  }));
}

function reportedStates(): string[] {
  return requests().map((request) => request.body.state);
}

/** A fresh copy of the module per test: its reporting state and heartbeat are module-level. */
async function loadPresence(): Promise<typeof import("./presence")> {
  vi.resetModules();
  return import("./presence");
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", fetchSpy);
  fetchSpy.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("presence reporting", () => {
  it("posts the initial state to the chat's presence route, with keepalive so a closed report can leave with the page", async () => {
    const presence = await loadPresence();

    presence.startPresenceReporting("agent-1", "client-1", "hidden");

    expect(requests()).toEqual([
      {
        url: "/api/agents/agent-1/presence",
        body: { client_id: "client-1", state: "hidden" },
        keepalive: true,
      },
    ]);
    expect(presence.currentPresenceState()).toBe("hidden");
  });

  it("reports nothing before reporting has started", async () => {
    const presence = await loadPresence();
    presence.reportPresence("visible");
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(presence.currentPresenceState()).toBe("visible");
  });

  it("reports every change and heartbeats the current state every minute", async () => {
    const presence = await loadPresence();
    presence.startPresenceReporting("agent-1", "client-1", "hidden");
    presence.reportPresence("visible");
    expect(reportedStates()).toEqual(["hidden", "visible"]);

    vi.advanceTimersByTime(60_000);
    expect(reportedStates()).toEqual(["hidden", "visible", "visible"]);

    presence.reportPresence("hidden");
    vi.advanceTimersByTime(60_000);
    expect(reportedStates()).toEqual(["hidden", "visible", "visible", "hidden", "hidden"]);
  });

  it("stops heartbeating once the page reported closed", async () => {
    const presence = await loadPresence();
    presence.startPresenceReporting("agent-1", "client-1", "visible");
    presence.reportPresence("closed");
    expect(reportedStates()).toEqual(["visible", "closed"]);

    vi.advanceTimersByTime(180_000);
    expect(reportedStates()).toEqual(["visible", "closed"]);
  });

  it("re-keys the reports when the shell hands the page another handshake, with one heartbeat", async () => {
    const presence = await loadPresence();
    presence.startPresenceReporting("agent-1", "client-1", "hidden");
    presence.startPresenceReporting("agent-1", "client-2", "visible");

    vi.advanceTimersByTime(60_000);

    expect(requests().map((request) => request.body)).toEqual([
      { client_id: "client-1", state: "hidden" },
      { client_id: "client-2", state: "visible" },
      { client_id: "client-2", state: "visible" },
    ]);
  });
});
