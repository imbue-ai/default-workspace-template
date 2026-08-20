import { afterEach, describe, expect, it, vi } from "vitest";

// apiUrl reads the base path from a <meta> tag, which vitest's node environment
// has no document for; identity keeps the asserted URLs the bare /api paths.
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import { loadWorkspaceGeometry, saveWorkspaceGeometry } from "./workspaceGeometry";
import type { RowGeometry } from "./rowGeometry";

const ROWS: RowGeometry[] = [
  { row_key: "turn-1", start_offset: 0, end_offset: 3, height: 160.5 },
  { row_key: "turn-2", start_offset: 3, end_offset: 51, height: 940 },
];

/** Stand in for the backend with one canned response for every call. */
function stubFetch(response: Partial<Response>): ReturnType<typeof vi.fn> {
  const mockFetch = vi.fn(() => Promise.resolve(response as Response));
  vi.stubGlobal("fetch", mockFetch);
  return mockFetch;
}

/** Stand in for a server that cannot be reached at all. */
function stubUnreachableFetch(): ReturnType<typeof vi.fn> {
  const mockFetch = vi.fn(() => Promise.reject(new Error("offline")));
  vi.stubGlobal("fetch", mockFetch);
  return mockFetch;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("loadWorkspaceGeometry", () => {
  it("asks for the width the geometry was measured at and returns the served rows", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ rows: ROWS }) });
    expect(await loadWorkspaceGeometry("agent-7", 12)).toEqual({ rows: ROWS });
    expect(mockFetch).toHaveBeenCalledWith("/api/agents/agent-7/geometry?width=12");
  });

  it("reads a transcript nobody has measured as absent rather than as empty geometry", async () => {
    // Reserving zero space for the whole conversation would collapse the
    // scrollbar; "not cached" leaves the estimate in place instead.
    stubFetch({ ok: true, json: () => Promise.resolve({ rows: [] }) });
    expect(await loadWorkspaceGeometry("agent-7", 12)).toBeNull();
  });

  it("degrades to no geometry when the server refuses or cannot be reached", async () => {
    stubFetch({ ok: false, json: () => Promise.resolve({}) });
    expect(await loadWorkspaceGeometry("agent-7", 12)).toBeNull();
    stubUnreachableFetch();
    expect(await loadWorkspaceGeometry("agent-7", 12)).toBeNull();
  });

  it("degrades to no geometry when the response is not a snapshot", async () => {
    // Stored geometry outlives the code that wrote it, so an unrecognized shape
    // must read as "measure it again" rather than throwing during a paint.
    stubFetch({ ok: true, json: () => Promise.resolve({ rows: "all of them" }) });
    expect(await loadWorkspaceGeometry("agent-7", 12)).toBeNull();
  });

  it("makes no request for a width bucket that names no viewport", async () => {
    // widthBucketFor quantizes, so a collapsed panel rounds to zero -- which the
    // server rejects, and which is not worth a round trip to be told.
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ rows: ROWS }) });
    expect(await loadWorkspaceGeometry("agent-7", 0)).toBeNull();
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

describe("saveWorkspaceGeometry", () => {
  it("files the measured rows under the width they were measured at", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ rows: ROWS }) });
    await saveWorkspaceGeometry("agent-7", 12, { rows: ROWS });
    expect(mockFetch).toHaveBeenCalledWith("/api/agents/agent-7/geometry", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ width: 12, rows: ROWS }),
    });
  });

  it("writes nothing when there is nothing measured to write", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ rows: [] }) });
    await saveWorkspaceGeometry("agent-7", 12, { rows: [] });
    await saveWorkspaceGeometry("agent-7", 0, { rows: ROWS });
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("swallows a failed write, because geometry is an optimisation", async () => {
    stubUnreachableFetch();
    await expect(saveWorkspaceGeometry("agent-7", 12, { rows: ROWS })).resolves.toBeUndefined();
  });
});
