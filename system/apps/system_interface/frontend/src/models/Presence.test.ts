// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  HEARTBEAT_INTERVAL_MS,
  applyPresence,
  getOwnIdentity,
  getPresentUsers,
  identityRefreshUrl,
  isSharedHost,
  resetPresenceForTesting,
  startPresenceHeartbeat,
} from "./Presence";
import type { PresentUser } from "./Presence";

interface RecordedRequest {
  url: string;
  body: string;
}

function jsonResponse(status: number, body?: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Stub `fetch` to answer every heartbeat with `response`, recording what was posted. */
function stubFetch(response: () => Response): RecordedRequest[] {
  const requests: RecordedRequest[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      requests.push({ url, body: String(init?.body ?? "") });
      return response();
    }),
  );
  return requests;
}

function setVisibility(state: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

const user = (overrides: Partial<PresentUser> = {}): PresentUser => ({
  user_id: "user-bob-4471",
  email: "bob@example.com",
  display_name: "Bob",
  avatar_url: null,
  owner: false,
  session_count: 1,
  first_seen: "2026-09-19T10:00:00.000000000Z",
  last_seen: "2026-09-19T10:00:00.000000000Z",
  ...overrides,
});

beforeEach(() => {
  vi.useFakeTimers();
  Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
});

afterEach(() => {
  resetPresenceForTesting();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("startPresenceHeartbeat", () => {
  it("heartbeats at once with a per-page session id, then every interval while visible", async () => {
    const requests = stubFetch(() => jsonResponse(200, { identity: { owner: true, user_id: "u1", email: "a@b" } }));

    startPresenceHeartbeat();
    await vi.advanceTimersByTimeAsync(0);
    expect(requests).toHaveLength(1);
    expect(requests[0].url).toContain("/api/presence/heartbeat");
    const firstSessionId = (JSON.parse(requests[0].body) as { session_id: string }).session_id;
    expect(firstSessionId.length).toBeGreaterThanOrEqual(8);

    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
    expect(requests).toHaveLength(2);
    expect((JSON.parse(requests[1].body) as { session_id: string }).session_id).toBe(firstSessionId);
    expect(getOwnIdentity()).toEqual({ owner: true, user_id: "u1", email: "a@b" });
  });

  it("stops while the tab is hidden and heartbeats again the moment it is visible", async () => {
    const requests = stubFetch(() => jsonResponse(204));
    startPresenceHeartbeat();
    await vi.advanceTimersByTimeAsync(0);
    expect(requests).toHaveLength(1);

    setVisibility("hidden");
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS * 3);
    expect(requests).toHaveLength(1);

    setVisibility("visible");
    await vi.advanceTimersByTimeAsync(0);
    expect(requests).toHaveLength(2);
  });

  it("forgets its identity when the workspace answers with none", async () => {
    let status = 200;
    stubFetch(() =>
      status === 200
        ? jsonResponse(200, { identity: { owner: true, user_id: "u1", email: "a@b" } })
        : jsonResponse(204),
    );
    startPresenceHeartbeat();
    await vi.advanceTimersByTimeAsync(0);
    expect(getOwnIdentity()).not.toBeNull();

    status = 204;
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
    expect(getOwnIdentity()).toBeNull();
  });

  it("beacons a leave carrying the same session id when the page goes away", async () => {
    const requests = stubFetch(() => jsonResponse(204));
    const beacons: { url: string; body: Blob }[] = [];
    vi.stubGlobal("navigator", {
      ...navigator,
      sendBeacon: (url: string, body: Blob) => {
        beacons.push({ url, body });
        return true;
      },
    });
    startPresenceHeartbeat();
    await vi.advanceTimersByTimeAsync(0);

    window.dispatchEvent(new Event("pagehide"));

    expect(beacons).toHaveLength(1);
    expect(beacons[0].url).toContain("/api/presence/leave");
    expect(await beacons[0].body.text()).toBe(requests[0].body);
  });

  it("survives a failed heartbeat and keeps going", async () => {
    let isFailing = true;
    const requests = stubFetch(() => {
      if (isFailing) throw new Error("offline");
      return jsonResponse(204);
    });
    startPresenceHeartbeat();
    await vi.advanceTimersByTimeAsync(0);
    isFailing = false;
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
    expect(requests).toHaveLength(2);
  });
});

describe("applyPresence", () => {
  it("replaces the connected set wholesale", () => {
    applyPresence([user(), user({ user_id: "user-owner-9c21", owner: true })]);
    expect(getPresentUsers().map((entry) => entry.user_id)).toEqual(["user-bob-4471", "user-owner-9c21"]);
    applyPresence([]);
    expect(getPresentUsers()).toEqual([]);
  });
});

describe("isSharedHost and identityRefreshUrl", () => {
  it("treats every local forward origin as not shared", () => {
    expect(isSharedHost("system_interface-abc.agent-0123.localhost:8421")).toBe(false);
    expect(isSharedHost("localhost:8000")).toBe(false);
    expect(isSharedHost("127.0.0.1:8000")).toBe(false);
    expect(isSharedHost("system_interface-abc.0123abcd.us1.imbueminds.com")).toBe(true);
  });

  it("links the refresh on the page's own origin with the page as next, and nowhere locally", () => {
    const shared = {
      host: "system_interface-abc.0123abcd.us1.imbueminds.com",
      origin: "https://system_interface-abc.0123abcd.us1.imbueminds.com",
      href: "https://system_interface-abc.0123abcd.us1.imbueminds.com/?tab=2",
    };
    expect(identityRefreshUrl(shared)).toBe(
      "https://system_interface-abc.0123abcd.us1.imbueminds.com/_auth/refresh?next=" + encodeURIComponent(shared.href),
    );
    expect(
      identityRefreshUrl({
        host: "agent-0123.localhost:8421",
        origin: "https://agent-0123.localhost:8421",
        href: "https://agent-0123.localhost:8421/",
      }),
    ).toBeNull();
  });
});
