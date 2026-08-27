import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// apiUrl reads the base path from a <meta> tag, which vitest's node environment
// has no document for; identity keeps the asserted URLs the bare /api paths.
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import {
  ensureVersionedAppsFresh,
  fetchVersionedAppNames,
  getVersionedAppNames,
  refreshVersionedApps,
  resetVersionedAppsForTesting,
} from "./VersionedApps";

/** Stand in for the backend with one canned response for every call. */
function stubFetch(response: Partial<Response>): ReturnType<typeof vi.fn> {
  const mockFetch = vi.fn(() => Promise.resolve(response as Response));
  vi.stubGlobal("fetch", mockFetch);
  return mockFetch;
}

/** The shape the versioning app's own ``GET /api/apps`` answers with -- an
 *  ``AppRef`` per folder under ``system/apps``, of which only the name matters
 *  here. Written out rather than reduced to names, so this test would notice
 *  the day that payload changes shape. */
function servedPayload(...names: string[]): { apps: { name: string; package_dir: string; title: string }[] } {
  return {
    apps: names.map((name) => ({ name, package_dir: `system/apps/${name}`, title: name })),
  };
}

beforeEach(() => {
  resetVersionedAppsForTesting();
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetVersionedAppsForTesting();
});

describe("the cached served list", () => {
  it("knows nothing until the first fetch lands", () => {
    // Null is "not known yet", which every menu reads as offering no History
    // row -- deliberately distinct from a known-empty list.
    expect(getVersionedAppNames()).toBeNull();
  });

  it("asks the shell's own backend rather than the versioning origin", async () => {
    // Sibling service origins are same-site but not same-origin and the
    // versioning app sends no CORS headers, so the request has to go through
    // the shell's passthrough.
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve(servedPayload("curio")) });
    await refreshVersionedApps();

    expect(mockFetch).toHaveBeenCalledWith("/api/versioned-apps");
  });

  it("keeps the names the versioning app says it serves", async () => {
    stubFetch({ ok: true, json: () => Promise.resolve(servedPayload("browser", "curio", "system-interface")) });
    await refreshVersionedApps();

    expect(getVersionedAppNames()).toEqual(new Set(["browser", "curio", "system-interface"]));
  });

  it("treats an unreachable backend as no answer, not as an empty answer", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("offline"))),
    );
    expect(await fetchVersionedAppNames()).toBeNull();
  });

  it("treats a refusal as no answer either", async () => {
    // A 503 is what the passthrough answers when the versioning service is not
    // registered or is down.
    stubFetch({ ok: false, status: 503, json: () => Promise.resolve({ detail: "unreachable" }) });
    expect(await fetchVersionedAppNames()).toBeNull();
  });

  it("ignores a body that is not the list it asked for", async () => {
    stubFetch({ ok: true, json: () => Promise.resolve({ detail: "something else" }) });
    expect(await fetchVersionedAppNames()).toBeNull();
  });

  it("keeps the last good answer when a later fetch fails", async () => {
    // A versioning service that stops after the list was read must not cost
    // the History rows of a workspace that had them a moment ago.
    stubFetch({ ok: true, json: () => Promise.resolve(servedPayload("curio")) });
    await refreshVersionedApps();
    vi.unstubAllGlobals();

    stubFetch({ ok: false, status: 503, json: () => Promise.resolve({}) });
    await refreshVersionedApps();

    expect(getVersionedAppNames()).toEqual(new Set(["curio"]));
  });

  it("shares one request among concurrent callers", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve(servedPayload("curio")) });
    await Promise.all([refreshVersionedApps(), refreshVersionedApps(), refreshVersionedApps()]);

    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
});

describe("ensureVersionedAppsFresh", () => {
  it("fetches when nothing is cached", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve(servedPayload("curio")) });
    await ensureVersionedAppsFresh();

    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(getVersionedAppNames()).toEqual(new Set(["curio"]));
  });

  it("does not refetch a fresh answer", async () => {
    // This rides along with every machine-inventory refresh, which happens on
    // every view mount and after every browser or terminal create -- the TTL
    // is what keeps that from being a request per click.
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve(servedPayload("curio")) });
    await ensureVersionedAppsFresh();
    await ensureVersionedAppsFresh();
    await ensureVersionedAppsFresh();

    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("retries after a failure rather than pinning the miss", async () => {
    // A failed fetch never advances the cache's age, so the next occasion asks
    // again -- which is what heals a workspace whose versioning service was
    // still starting when the page loaded.
    stubFetch({ ok: false, status: 503, json: () => Promise.resolve({}) });
    await ensureVersionedAppsFresh();
    expect(getVersionedAppNames()).toBeNull();
    vi.unstubAllGlobals();

    stubFetch({ ok: true, json: () => Promise.resolve(servedPayload("curio")) });
    await ensureVersionedAppsFresh();

    expect(getVersionedAppNames()).toEqual(new Set(["curio"]));
  });

  it("refetches once the cached answer ages past its TTL", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve(servedPayload("curio")) });
    await ensureVersionedAppsFresh();

    vi.useFakeTimers();
    try {
      vi.setSystemTime(Date.now() + 61_000);
      await ensureVersionedAppsFresh();
    } finally {
      vi.useRealTimers();
    }

    expect(mockFetch).toHaveBeenCalledTimes(2);
  });
});

describe("the cold-start retry", () => {
  /** Run out every pending retry timer, awaiting the fetch each one starts.
   *  Bounded well above the retry limit, so a runaway schedule fails the test
   *  by tripping the assertions below rather than by hanging. */
  async function runPendingRetries(rounds = 12): Promise<void> {
    for (let round = 0; round < rounds; round += 1) {
      await vi.advanceTimersByTimeAsync(60_000);
    }
  }

  it("asks again by itself while it has never had an answer", async () => {
    // The window this exists for: the shell loaded while the versioning service
    // was still starting. Nothing else would ask again until the user mounted a
    // view or opened a launcher, so the History rows would simply be missing
    // from a workspace that has them.
    vi.useFakeTimers();
    try {
      stubFetch({ ok: false, status: 503, json: () => Promise.resolve({}) });
      await refreshVersionedApps();
      expect(getVersionedAppNames()).toBeNull();

      vi.unstubAllGlobals();
      stubFetch({ ok: true, json: () => Promise.resolve(servedPayload("curio")) });
      await vi.advanceTimersByTimeAsync(60_000);

      expect(getVersionedAppNames()).toEqual(new Set(["curio"]));
    } finally {
      vi.useRealTimers();
    }
  });

  it("gives up after a bounded handful, rather than polling a service that is simply gone", async () => {
    // A workspace running no versioning service at all must not be left
    // hammering the endpoint for the rest of the session.
    vi.useFakeTimers();
    try {
      const mockFetch = stubFetch({ ok: false, status: 503, json: () => Promise.resolve({}) });
      await refreshVersionedApps();
      await runPendingRetries();

      // The first call plus the allowance, and nothing after it.
      expect(mockFetch).toHaveBeenCalledTimes(7);
      const settled = mockFetch.mock.calls.length;
      await runPendingRetries();
      expect(mockFetch).toHaveBeenCalledTimes(settled);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops retrying for good once any answer lands", async () => {
    // And stays stopped through a LATER failure: that one keeps the answer that
    // landed, which is not the cold start this is for.
    vi.useFakeTimers();
    try {
      stubFetch({ ok: true, json: () => Promise.resolve(servedPayload("curio")) });
      await refreshVersionedApps();
      vi.unstubAllGlobals();

      const mockFetch = stubFetch({ ok: false, status: 503, json: () => Promise.resolve({}) });
      await refreshVersionedApps();
      await runPendingRetries();

      expect(mockFetch).toHaveBeenCalledTimes(1);
      expect(getVersionedAppNames()).toEqual(new Set(["curio"]));
    } finally {
      vi.useRealTimers();
    }
  });
});
