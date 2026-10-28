// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  HEARTBEAT_INTERVAL_MS,
  applyPresence,
  getOwnIdentity,
  getOwnParticipant,
  getPresentUsers,
  resetPresenceForTesting,
  startPresenceHeartbeat,
} from "./Presence";
import { presentUserRecord } from "../testing/records";

interface RecordedRequest {
  url: string;
  method: string;
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
      requests.push({ url, method: String(init?.method ?? "GET"), body: String(init?.body ?? "") });
      return response();
    }),
  );
  return requests;
}

function setVisibility(state: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

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
  it("posts a heartbeat at once, then every interval while visible, carrying the newest input it saw", async () => {
    const requests = stubFetch(() =>
      jsonResponse(200, { identity: { owner: true, user_id: "u1", email: "a@b" }, participant: "user:u1" }),
    );

    startPresenceHeartbeat();
    await vi.advanceTimersByTimeAsync(0);
    expect(requests).toHaveLength(1);
    expect(requests[0].url).toContain("/api/presence/heartbeat");
    expect(requests[0].method).toBe("POST");
    expect(JSON.parse(requests[0].body)).toEqual({ last_input_at: null });

    vi.setSystemTime(new Date("2026-10-22T10:00:05Z"));
    window.dispatchEvent(new Event("keydown"));
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
    expect(requests).toHaveLength(2);
    expect(JSON.parse(requests[1].body)).toEqual({ last_input_at: "2026-10-22T10:00:05.000Z" });
    expect(getOwnIdentity()).toEqual({ owner: true, user_id: "u1", email: "a@b", participant: "user:u1" });
    expect(getOwnParticipant()).toBe("user:u1");
  });

  it("knows the owner of an unshared workspace by the owner key, with no account", async () => {
    stubFetch(() => jsonResponse(200, { identity: { owner: true }, participant: "owner" }));
    startPresenceHeartbeat();
    await vi.advanceTimersByTimeAsync(0);
    expect(getOwnIdentity()).toEqual({ owner: true, user_id: null, email: null, participant: "owner" });
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
        ? jsonResponse(200, { identity: { owner: true, user_id: "u1", email: "a@b" }, participant: "user:u1" })
        : jsonResponse(204),
    );
    startPresenceHeartbeat();
    await vi.advanceTimersByTimeAsync(0);
    expect(getOwnIdentity()).not.toBeNull();

    status = 204;
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
    expect(getOwnIdentity()).toBeNull();
  });

  it("sends nothing when the page goes away: a stopped heartbeat is the leave", async () => {
    const requests = stubFetch(() => jsonResponse(204));
    const beacons: string[] = [];
    vi.stubGlobal("navigator", {
      ...navigator,
      sendBeacon: (url: string) => {
        beacons.push(url);
        return true;
      },
    });
    startPresenceHeartbeat();
    await vi.advanceTimersByTimeAsync(0);

    window.dispatchEvent(new Event("pagehide"));
    await vi.advanceTimersByTimeAsync(0);

    expect(beacons).toEqual([]);
    expect(requests).toHaveLength(1);
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
    applyPresence([presentUserRecord("user-bob-4471"), presentUserRecord("user-owner-9c21", { owner: true })]);
    expect(getPresentUsers().map((entry) => entry.user_id)).toEqual(["user-bob-4471", "user-owner-9c21"]);
    applyPresence([]);
    expect(getPresentUsers()).toEqual([]);
  });
});
