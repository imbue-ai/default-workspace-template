import { describe, expect, it } from "vitest";
import { isDeepLinkEmpty, parseDeepLink, stripDeepLinkParams } from "./deepLinks";

describe("parseDeepLink", () => {
  it("reads the desktop, the open target, and the launch target", () => {
    const link = parseDeepLink("?desktop=home&open=docs%3A%2F%3Fdoc%3D1&launch=notes%3Anew");
    expect(link).toEqual({
      desktopId: "home",
      open: { app: "docs", path: "/?doc=1" },
      launch: { app: "notes", launch: "new" },
    });
    expect(isDeepLinkEmpty(link)).toBe(false);
  });

  it("ignores a malformed target and reads an empty query as nothing", () => {
    expect(parseDeepLink("?open=docs&launch=:new")).toEqual({ desktopId: null, open: null, launch: null });
    expect(parseDeepLink("?open=docs:nowhere")).toEqual({ desktopId: null, open: null, launch: null });
    expect(isDeepLinkEmpty(parseDeepLink(""))).toBe(true);
  });
});

describe("stripDeepLinkParams", () => {
  it("removes only the deep-link parameters", () => {
    expect(stripDeepLinkParams("?desktop=home&open=docs%3A%2F&launch=notes%3Anew&other=1")).toBe("?other=1");
    expect(stripDeepLinkParams("?desktop=home")).toBe("");
  });
});
