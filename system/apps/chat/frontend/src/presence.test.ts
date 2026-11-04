// @vitest-environment jsdom
/**
 * The chat page's presence reports: what the chat app's presence route is told, and when
 * (the initial state, every change of state or focus, a heartbeat of the current state,
 * nothing after closed), always under the one instance id of this page load.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@imbue/workspace-ui/src/base-path", () => ({
  apiUrl: (path: string) => path,
}));

interface PresenceBody {
  instance_id: string;
  client_id: string;
  state: string;
  is_focused: boolean;
  sequence: number;
}

interface PresenceRequest {
  url: string;
  body: PresenceBody;
  keepalive: boolean | undefined;
}

const fetchSpy = vi.fn(() => Promise.resolve(new Response()));
let hasFocus = false;

function requests(): PresenceRequest[] {
  return (fetchSpy.mock.calls as unknown as Array<[string, RequestInit]>).map(([url, init]) => ({
    url,
    body: JSON.parse(init.body as string) as PresenceBody,
    keepalive: init.keepalive,
  }));
}

function reportedStates(): string[] {
  return requests().map((request) => request.body.state);
}

/** A fresh copy of the module per test: its reporting state, heartbeat, and instance id are module-level. */
async function loadPresence(): Promise<typeof import("./presence")> {
  vi.resetModules();
  return import("./presence");
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", fetchSpy);
  fetchSpy.mockClear();
  hasFocus = false;
  vi.spyOn(document, "hasFocus").mockImplementation(() => hasFocus);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("presence reporting", () => {
  it("posts the initial state to the chat's presence route, with keepalive so a closed report can leave with the page", async () => {
    const presence = await loadPresence();

    presence.startPresenceReporting("agent-1", "client-1", "hidden");

    expect(requests()).toHaveLength(1);
    const [{ url, keepalive, body }] = requests();
    expect(url).toBe("/api/chats/agent-1/presence");
    expect(keepalive).toBe(true);
    const { instance_id: instanceId, ...rest } = body;
    expect(rest).toEqual({ client_id: "client-1", state: "hidden", is_focused: false, sequence: 1 });
    expect(instanceId).toMatch(/^[0-9a-f-]{36}$/);
    expect(presence.currentPresenceState()).toBe("hidden");
  });

  it("reports nothing before reporting has started", async () => {
    const presence = await loadPresence();
    presence.reportPresence("visible");
    presence.reportFocusChange();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(presence.currentPresenceState()).toBe("visible");
  });

  it("reports every change and heartbeats the current state every thirty seconds", async () => {
    const presence = await loadPresence();
    presence.startPresenceReporting("agent-1", "client-1", "hidden");
    presence.reportPresence("visible");
    expect(reportedStates()).toEqual(["hidden", "visible"]);

    vi.advanceTimersByTime(29_999);
    expect(reportedStates()).toEqual(["hidden", "visible"]);
    vi.advanceTimersByTime(1);
    expect(reportedStates()).toEqual(["hidden", "visible", "visible"]);

    presence.reportPresence("hidden");
    vi.advanceTimersByTime(30_000);
    expect(reportedStates()).toEqual(["hidden", "visible", "visible", "hidden", "hidden"]);
  });

  it("reports the document's focus as it is gained and lost, and in every heartbeat", async () => {
    const presence = await loadPresence();
    presence.startPresenceReporting("agent-1", "client-1", "visible");

    hasFocus = true;
    presence.reportFocusChange();
    hasFocus = false;
    presence.reportFocusChange();
    hasFocus = true;
    vi.advanceTimersByTime(30_000);

    expect(requests().map((request) => [request.body.state, request.body.is_focused])).toEqual([
      ["visible", false],
      ["visible", true],
      ["visible", false],
      ["visible", true],
    ]);
  });

  it("numbers every report in the order it is sent, heartbeats and a re-keyed client included", async () => {
    const presence = await loadPresence();
    presence.startPresenceReporting("agent-1", "client-1", "hidden");
    presence.reportPresence("visible");
    presence.reportFocusChange();
    vi.advanceTimersByTime(30_000);
    presence.startPresenceReporting("agent-1", "client-2", "visible");
    presence.reportPresence("closed");

    expect(requests().map((request) => request.body.sequence)).toEqual([1, 2, 3, 4, 5, 6]);
  });

  it("stops reporting once the page reported closed", async () => {
    const presence = await loadPresence();
    presence.startPresenceReporting("agent-1", "client-1", "visible");
    presence.reportPresence("closed");
    expect(reportedStates()).toEqual(["visible", "closed"]);

    presence.reportFocusChange();
    vi.advanceTimersByTime(180_000);
    expect(reportedStates()).toEqual(["visible", "closed"]);
  });

  it("keeps one instance id for the page's life, through a re-keyed client, and mints a new one per page load", async () => {
    const presence = await loadPresence();
    presence.startPresenceReporting("agent-1", "client-1", "hidden");
    presence.startPresenceReporting("agent-1", "client-2", "visible");
    presence.reportFocusChange();
    vi.advanceTimersByTime(30_000);

    const bodies = requests().map((request) => request.body);
    expect(bodies.map((body) => body.client_id)).toEqual(["client-1", "client-2", "client-2", "client-2"]);
    expect(new Set(bodies.map((body) => body.instance_id)).size).toBe(1);

    fetchSpy.mockClear();
    const reloaded = await loadPresence();
    reloaded.startPresenceReporting("agent-1", "client-2", "visible");
    expect(requests()[0].body.instance_id).not.toBe(bodies[0].instance_id);
  });
});
