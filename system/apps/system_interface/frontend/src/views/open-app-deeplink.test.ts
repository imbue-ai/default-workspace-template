import { describe, expect, it } from "vitest";
import { openAppNameFromSearch, searchWithoutOpenApp } from "./open-app-deeplink";

describe("openAppNameFromSearch", () => {
  it("extracts the requested app name", () => {
    expect(openAppNameFromSearch("?open_app=slack-imbue")).toBe("slack-imbue");
  });

  it("decodes a percent-encoded name", () => {
    expect(openAppNameFromSearch("?open_app=a%20b%2Fc")).toBe("a b/c");
  });

  it("returns null when absent or empty", () => {
    expect(openAppNameFromSearch("")).toBeNull();
    expect(openAppNameFromSearch("?other=1")).toBeNull();
    expect(openAppNameFromSearch("?open_app=")).toBeNull();
  });
});

describe("searchWithoutOpenApp", () => {
  it("strips only the open_app parameter", () => {
    expect(searchWithoutOpenApp("?open_app=web&layout=dev")).toBe("?layout=dev");
  });

  it("returns an empty string when nothing else remains", () => {
    expect(searchWithoutOpenApp("?open_app=web")).toBe("");
  });
});
