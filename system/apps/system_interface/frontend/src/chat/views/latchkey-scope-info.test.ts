import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// apiUrl reads the base path from a <meta> tag, which vitest's node environment
// has no document for; identity keeps the fetched URLs the bare /api paths.
vi.mock("../../base-path", () => ({ apiUrl: (path: string) => path }));

// m.redraw needs a real render loop; stub it so the cache logic runs without a DOM.
const { mockRedraw } = vi.hoisted(() => ({ mockRedraw: vi.fn() }));
vi.mock("mithril", () => ({
  default: { redraw: mockRedraw },
}));

import type { ScopeInfo } from "./latchkey-scope-info";
import { SCOPE_INFO_RETRY_DELAY_MS, getScopeInfo, resetScopeInfoCacheForTesting } from "./latchkey-scope-info";

const GMAIL_INFO: ScopeInfo = {
  scope: "google-gmail-api",
  display_name: "Gmail",
  description: null,
  permissions: [],
};

function okResponse(body: unknown): { ok: boolean; status: number; json: () => Promise<unknown> } {
  return { ok: true, status: 200, json: async () => body };
}

function errorResponse(status: number): { ok: boolean; status: number; json: () => Promise<unknown> } {
  return { ok: false, status, json: async () => ({}) };
}

/** Let the fire-and-forget fetch chain inside getScopeInfo settle. */
async function flushFetches(): Promise<void> {
  for (let hop = 0; hop < 6; hop += 1) {
    await Promise.resolve();
  }
}

beforeEach(() => {
  resetScopeInfoCacheForTesting();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("getScopeInfo", () => {
  it("resolves a scope to its catalog info after the background fetch lands", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(GMAIL_INFO));
    vi.stubGlobal("fetch", fetchMock);

    expect(getScopeInfo("google-gmail-api")).toBeNull();
    await flushFetches();

    expect(getScopeInfo("google-gmail-api")).toEqual(GMAIL_INFO);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("caches a 404 as a definitive null and never refetches", async () => {
    const fetchMock = vi.fn().mockResolvedValue(errorResponse(404));
    vi.stubGlobal("fetch", fetchMock);

    expect(getScopeInfo("not-a-scope")).toBeNull();
    await flushFetches();
    vi.advanceTimersByTime(SCOPE_INFO_RETRY_DELAY_MS * 2);

    expect(getScopeInfo("not-a-scope")).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retries a transient gateway failure and resolves once the gateway is back", async () => {
    // First render hits the gateway-restart window (502); the card must not be
    // pinned to the raw scope forever.
    const fetchMock = vi.fn().mockResolvedValueOnce(errorResponse(502)).mockResolvedValue(okResponse(GMAIL_INFO));
    vi.stubGlobal("fetch", fetchMock);

    expect(getScopeInfo("google-gmail-api")).toBeNull();
    await flushFetches();

    // Before the retry delay passes, the failure is not retried on render.
    expect(getScopeInfo("google-gmail-api")).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(SCOPE_INFO_RETRY_DELAY_MS);
    expect(getScopeInfo("google-gmail-api")).toBeNull();
    await flushFetches();

    expect(getScopeInfo("google-gmail-api")).toEqual(GMAIL_INFO);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("treats a network error like a transient failure", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("connection refused"))
      .mockResolvedValue(okResponse(GMAIL_INFO));
    vi.stubGlobal("fetch", fetchMock);

    expect(getScopeInfo("google-gmail-api")).toBeNull();
    await flushFetches();
    vi.advanceTimersByTime(SCOPE_INFO_RETRY_DELAY_MS);

    expect(getScopeInfo("google-gmail-api")).toBeNull();
    await flushFetches();

    expect(getScopeInfo("google-gmail-api")).toEqual(GMAIL_INFO);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
