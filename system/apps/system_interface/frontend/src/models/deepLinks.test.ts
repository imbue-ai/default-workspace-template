import { describe, expect, it } from "vitest";

import { isDeepLinkEmpty, parseDeepLink, stripDeepLinkParams } from "./deepLinks";

describe("parseDeepLink", () => {
  it("reads the view, the address to open, and the action as app and action id", () => {
    const link = parseDeepLink("?view=research&open=app%3Afiles%3Finstance%3Dfiles-2&action=terminal%3Anew");
    expect(link).toEqual({
      viewId: "research",
      openAddress: "app:files?instance=files-2",
      action: { app: "terminal", actionId: "new" },
    });
    expect(isDeepLinkEmpty(link)).toBe(false);
  });

  it("carries nothing for a plain page load or a malformed action", () => {
    expect(isDeepLinkEmpty(parseDeepLink(""))).toBe(true);
    expect(isDeepLinkEmpty(parseDeepLink("?other=1"))).toBe(true);
    expect(parseDeepLink("?action=terminal").action).toBeNull();
    expect(parseDeepLink("?action=terminal:").action).toBeNull();
    expect(parseDeepLink("?action=:new").action).toBeNull();
    expect(parseDeepLink("?view=").viewId).toBeNull();
  });
});

describe("stripDeepLinkParams", () => {
  it("removes the deep-link parameters and keeps the rest", () => {
    expect(stripDeepLinkParams("?view=research&open=app%3Afiles&action=terminal%3Anew&follow=c1")).toBe("");
    expect(stripDeepLinkParams("?view=research&debug=1")).toBe("?debug=1");
    expect(stripDeepLinkParams("")).toBe("");
  });
});
