import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import {
  ensureVersionedAppsFresh,
  fetchVersionedAppNames,
  getVersionedAppNames,
  refreshVersionedApps,
  resetVersionedAppsForTesting,
} from "./VersionedApps";

function stubFetch(response: Partial<Response>): ReturnType<typeof vi.fn> {
  const mockFetch = vi.fn(() => Promise.resolve(response as Response));
  vi.stubGlobal("fetch", mockFetch);
  return mockFetch;
}

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
  it("asks the shell's own backend rather than the versioning origin", async () => {
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
    stubFetch({ ok: false, status: 503, json: () => Promise.resolve({ detail: "unreachable" }) });
    expect(await fetchVersionedAppNames()).toBeNull();
  });

  it("ignores a body that is not the list it asked for", async () => {
    stubFetch({ ok: true, json: () => Promise.resolve({ detail: "something else" }) });
    expect(await fetchVersionedAppNames()).toBeNull();
  });

  it("keeps the last good answer when a later fetch fails", async () => {
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
  it("does not refetch a fresh answer", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve(servedPayload("curio")) });
    await ensureVersionedAppsFresh();
    await ensureVersionedAppsFresh();
    await ensureVersionedAppsFresh();

    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("retries after a failure rather than pinning the miss", async () => {
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
  async function runPendingRetries(rounds = 12): Promise<void> {
    for (let round = 0; round < rounds; round += 1) {
      await vi.advanceTimersByTimeAsync(60_000);
    }
  }

  it("asks again by itself while it has never had an answer", async () => {
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
    vi.useFakeTimers();
    try {
      const mockFetch = stubFetch({ ok: false, status: 503, json: () => Promise.resolve({}) });
      await refreshVersionedApps();
      await runPendingRetries();

      expect(mockFetch).toHaveBeenCalledTimes(7);
      const settled = mockFetch.mock.calls.length;
      await runPendingRetries();
      expect(mockFetch).toHaveBeenCalledTimes(settled);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops retrying for good once any answer lands", async () => {
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
