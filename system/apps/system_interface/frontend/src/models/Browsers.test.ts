import { afterEach, describe, expect, it, vi } from "vitest";

// apiUrl reads the base path from a <meta> tag, which vitest's node environment
// has no document for; identity keeps the asserted URLs the bare /api paths.
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import { createBrowser, validateBrowserName } from "./Browsers";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("validateBrowserName", () => {
  it("accepts lowercase alnum words joined by single dashes", () => {
    expect(validateBrowserName("alex-smith")).toBeNull();
    expect(validateBrowserName("browser-2")).toBeNull();
    expect(validateBrowserName("a")).toBeNull();
  });

  it("rejects everything the daemon's is_valid_browser_name rejects", () => {
    // Mirrors the daemon's rule: lowercase alnum words joined by single dashes,
    // 1..40 chars, no leading/trailing/double dash, not all-digits. The create
    // flow guards the machine-minted name with this before opening a pane.
    const invalidNames = [
      "",
      "Has-Caps",
      "has_underscore",
      "has space",
      "-leading",
      "trailing-",
      "double--dash",
      "tr" + "a".repeat(40), // 42 chars: over the 40-char limit
      "123",
      "name!",
    ];
    for (const bad of invalidNames) {
      expect(validateBrowserName(bad), bad).not.toBeNull();
    }
  });
});

describe("createBrowser", () => {
  it("posts {name} to the daemon and reports the daemon's final name", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ name: "alex-smith", key_available: true }) });
    vi.stubGlobal("fetch", fetchMock);

    expect(await createBrowser("alex-smith")).toEqual({ ok: true, name: "alex-smith", reason: "" });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/browsers",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ name: "alex-smith" }) }),
    );
  });

  it("carries the daemon's reason out of a rejection instead of throwing", async () => {
    // 400 invalid / 409 duplicate-or-full / 503 installing: the caller has an
    // optimistic pane open, so the refusal comes back as a value it can act on.
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({ error: "3/3 browsers open -- close one first." }),
    });
    vi.stubGlobal("fetch", fetchMock);

    expect(await createBrowser("my-browser")).toEqual({
      ok: false,
      name: "my-browser",
      reason: "3/3 browsers open -- close one first.",
    });
  });

  it("falls back to a generic reason when the daemon error body is missing", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    expect((await createBrowser("my-browser")).reason).toBe("The browser could not be created.");
  });

  it("reports a network failure with a human-readable reason so it is never silent", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("network down"))),
    );

    const result = await createBrowser("my-browser");
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/Could not reach the browser service/);
  });
});
