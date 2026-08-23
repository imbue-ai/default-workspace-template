import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// apiUrl reads the base path from a <meta> tag, which vitest's node environment
// has no document for; identity keeps the asserted URLs the bare /api paths.
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import {
  allocateAppInstance,
  appInstanceDisplayName,
  fetchAppInstances,
  getAppInstances,
  instancesOfService,
  refreshAppInstances,
} from "./AppInstances";

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

/** Put the module-level cache back to "no instances anywhere". The cache is
 *  machine-wide state, so each test has to start from the same place. */
async function resetCache(): Promise<void> {
  stubFetch({ ok: true, json: () => Promise.resolve({ instances: {} }) });
  await refreshAppInstances();
  vi.unstubAllGlobals();
}

beforeEach(async () => {
  await resetCache();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the cached inventory", () => {
  it("loads the machine's instances and answers from them", async () => {
    stubFetch({ ok: true, json: () => Promise.resolve({ instances: { files: ["files-1", "files-3"] } }) });
    await refreshAppInstances();

    expect(getAppInstances()).toEqual({ files: ["files-1", "files-3"] });
    expect(instancesOfService("files")).toEqual(["files-1", "files-3"]);
  });

  it("answers an absent service with no instances", () => {
    expect(instancesOfService("docs")).toEqual([]);
  });

  it("treats an unreachable server as a machine with no instances", async () => {
    stubUnreachableFetch();
    expect(await fetchAppInstances()).toEqual({});
  });
});

describe("allocateAppInstance", () => {
  it("posts to the app's allocator and answers the minted name", async () => {
    const mockFetch = stubFetch({
      ok: true,
      json: () => Promise.resolve({ name: "files", instance: "files-2", ref: "service:files?instance=files-2" }),
    });

    await expect(allocateAppInstance("files")).resolves.toBe("files-2");
    expect(mockFetch).toHaveBeenCalledWith("/api/apps/files/instances/allocate", { method: "POST" });
  });

  it("surfaces the server's detail on a refusal", async () => {
    stubFetch({ ok: false, status: 404, json: () => Promise.resolve({ detail: "No registered app named 'x'" }) });

    await expect(allocateAppInstance("x")).rejects.toThrow("No registered app named 'x'");
  });

  it("falls back to the HTTP status when the refusal carries no body", async () => {
    stubFetch({ ok: false, status: 500, json: () => Promise.reject(new Error("no body")) });

    await expect(allocateAppInstance("files")).rejects.toThrow("HTTP 500");
  });

  it("rejects a success that carries no minted name", async () => {
    stubFetch({ ok: true, json: () => Promise.resolve({}) });

    await expect(allocateAppInstance("files")).rejects.toThrow("Instance allocation returned no name");
  });
});

describe("appInstanceDisplayName", () => {
  it("derives the app's display name plus the instance number", () => {
    expect(appInstanceDisplayName("File Viewer", "files-2")).toBe("File Viewer 2");
    // The app's own chosen name rides into its instances' names.
    expect(appInstanceDisplayName("Docs", "files-1")).toBe("Docs 1");
  });

  it("keeps a name that does not parse, prefixed, so it still says what it belongs to", () => {
    expect(appInstanceDisplayName("File Viewer", "hand-edited")).toBe("File Viewer hand-edited");
  });
});
