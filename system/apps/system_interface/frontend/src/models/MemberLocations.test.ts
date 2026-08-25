import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// apiUrl reads the base path from a <meta> tag, which vitest's node environment
// has no document for; identity keeps the asserted URLs the bare /api paths.
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import {
  applyMemberLocationChange,
  fetchMemberLocations,
  getMemberLocation,
  loadMemberLocations,
  recordMemberLocation,
} from "./MemberLocations";

const FILES_2 = "service:files?instance=files-2";

/** Stand in for the backend with one canned response for every call. */
function stubFetch(response: Partial<Response>): ReturnType<typeof vi.fn> {
  const mockFetch = vi.fn(() => Promise.resolve(response as Response));
  vi.stubGlobal("fetch", mockFetch);
  return mockFetch;
}

/** Stand in for a server that cannot be reached at all. */
function stubUnreachableFetch(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new Error("offline"))),
  );
}

/** Put the module-level cache back to "nothing has beaconed". The cache is
 *  machine-wide state, so each test has to start from the same place. */
async function resetCache(): Promise<void> {
  stubFetch({ ok: true, json: () => Promise.resolve({ locations: {} }) });
  await loadMemberLocations();
  vi.unstubAllGlobals();
}

beforeEach(async () => {
  await resetCache();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the cached map", () => {
  it("loads the machine's locations and answers from them", async () => {
    stubFetch({ ok: true, json: () => Promise.resolve({ locations: { [FILES_2]: "/notes/2026/" } }) });
    await loadMemberLocations();

    expect(getMemberLocation(FILES_2)).toBe("/notes/2026/");
  });

  it("answers null for an object that has never beaconed", () => {
    expect(getMemberLocation(FILES_2)).toBeNull();
  });

  it("treats an unreachable server as a machine where nothing has beaconed", async () => {
    stubUnreachableFetch();
    expect(await fetchMemberLocations()).toEqual({});
  });

  it("takes a broadcast location, and drops the entry a destroy carries", () => {
    applyMemberLocationChange(FILES_2, "/notes/");
    expect(getMemberLocation(FILES_2)).toBe("/notes/");

    // Null is "the entry was dropped" (the object was destroyed, or it
    // beaconed a blank): a reused ref must not inherit a dead one's folder.
    applyMemberLocationChange(FILES_2, null);
    expect(getMemberLocation(FILES_2)).toBeNull();
  });

  it("treats an empty broadcast path as a drop too", () => {
    applyMemberLocationChange(FILES_2, "/notes/");
    applyMemberLocationChange(FILES_2, "");
    expect(getMemberLocation(FILES_2)).toBeNull();
  });
});

describe("recordMemberLocation", () => {
  it("posts the ref and path, and caches the path the server kept", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ ref: FILES_2, path: "/kept/" }) });

    recordMemberLocation(FILES_2, "/kept/");
    await vi.waitFor(() => {
      expect(getMemberLocation(FILES_2)).toBe("/kept/");
    });

    expect(mockFetch).toHaveBeenCalledWith("/api/member-locations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: FILES_2, path: "/kept/" }),
    });
  });

  it("drops the cached entry when the server answers that it was cleared", async () => {
    applyMemberLocationChange(FILES_2, "/stale/");
    stubFetch({ ok: true, json: () => Promise.resolve({ ref: FILES_2, path: null }) });

    recordMemberLocation(FILES_2, "  ");
    await vi.waitFor(() => {
      expect(getMemberLocation(FILES_2)).toBeNull();
    });
  });

  it("leaves the cache alone on a rejected post", async () => {
    const mockFetch = stubFetch({ ok: false, status: 400, json: () => Promise.resolve({ detail: "bad path" }) });

    recordMemberLocation(FILES_2, "not-rooted");
    await vi.waitFor(() => {
      expect(mockFetch).toHaveBeenCalled();
    });

    expect(getMemberLocation(FILES_2)).toBeNull();
  });

  it("swallows an unreachable server, because a beacon is fire-and-forget", () => {
    stubUnreachableFetch();

    expect(() => {
      recordMemberLocation(FILES_2, "/notes/");
    }).not.toThrow();
    expect(getMemberLocation(FILES_2)).toBeNull();
  });
});
