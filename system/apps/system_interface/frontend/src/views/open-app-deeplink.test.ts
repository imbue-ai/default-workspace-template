import { describe, expect, it } from "vitest";
import { openAppNameFromSearch } from "./open-app-deeplink";

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

  it("reads the name alongside other parameters", () => {
    expect(openAppNameFromSearch("?layout=dev&open_app=web")).toBe("web");
  });
});
