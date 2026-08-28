import { afterEach, describe, expect, it, vi } from "vitest";

// apiUrl reads the base path from a <meta> tag, which vitest's node environment
// has no document for; identity keeps the asserted URLs the bare /api paths.
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import { createBrowser } from "./Browsers";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("createBrowser", () => {
  it("posts an empty body and reports the daemon's minted name", async () => {
    // The daemon mints the name (the first free browser-<N>); the client sends
    // no name of its own and takes whatever came back as the identity to open.
    const fetchMock = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => ({ name: "browser-1" }) });
    vi.stubGlobal("fetch", fetchMock);

    expect(await createBrowser()).toEqual({ ok: true, name: "browser-1", reason: "" });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/browsers",
      expect.objectContaining({ method: "POST", body: JSON.stringify({}) }),
    );
  });

  it("carries the daemon's reason out of a rejection instead of throwing", async () => {
    // 400 invalid / 409 duplicate-or-full / 503 installing: the refusal comes
    // back as a value the caller surfaces (nothing was opened yet).
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({ error: "3/3 browsers open -- close one first." }),
    });
    vi.stubGlobal("fetch", fetchMock);

    expect(await createBrowser()).toEqual({
      ok: false,
      name: "",
      reason: "3/3 browsers open -- close one first.",
    });
  });

  it("treats a success with no name in the body as a failure", async () => {
    // The name IS the result: a create that cannot say what it made gives the
    // caller nothing to open, so it must walk the failure branch.
    const fetchMock = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    const result = await createBrowser();
    expect(result.ok).toBe(false);
    expect(result.name).toBe("");
  });

  it("falls back to a generic reason when the daemon error body is missing", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    expect((await createBrowser()).reason).toBe("The browser could not be created.");
  });

  it("reports a network failure with a human-readable reason so it is never silent", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("network down"))),
    );

    const result = await createBrowser();
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/Could not reach the browser service/);
  });
});
