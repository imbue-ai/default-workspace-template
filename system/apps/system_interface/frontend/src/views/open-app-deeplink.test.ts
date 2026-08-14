import { describe, expect, it } from "vitest";
import { openAppNameFromQuery } from "./open-app-deeplink";

describe("openAppNameFromQuery", () => {
  it("extracts the requested app name", () => {
    expect(openAppNameFromQuery("?open_app=slack-imbue")).toBe("slack-imbue");
  });

  it("decodes a percent-encoded name", () => {
    expect(openAppNameFromQuery("?open_app=a%20b%2Fc")).toBe("a b/c");
  });

  it("returns null when absent or empty", () => {
    expect(openAppNameFromQuery("")).toBeNull();
    expect(openAppNameFromQuery("?other=1")).toBeNull();
    expect(openAppNameFromQuery("?open_app=")).toBeNull();
  });

  it("reads the name alongside other parameters", () => {
    expect(openAppNameFromQuery("?layout=dev&open_app=web")).toBe("web");
  });
});
