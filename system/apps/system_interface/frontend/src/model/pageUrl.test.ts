import { describe, expect, it } from "vitest";
import { labelForApp, windowPageUrl } from "./pageUrl";

const app = { name: "docs", label: "docs-x7k9q2w1", url: "http://127.0.0.1:7001/" };

describe("windowPageUrl", () => {
  it("derives the app's origin from its label on a workspace host", () => {
    expect(windowPageUrl(app, "/?doc=1", "shell-ab12.host-0123abcd.localhost:8421", "http:")).toBe(
      "http://docs-x7k9q2w1.host-0123abcd.localhost:8421/?doc=1",
    );
  });

  it("uses the registered loopback url on a host with no workspace coordinate", () => {
    expect(windowPageUrl(app, "/new", "127.0.0.1:8000", "http:")).toBe("http://127.0.0.1:7001/new");
    expect(windowPageUrl(app, "new", "127.0.0.1:8000", "http:")).toBe("http://127.0.0.1:7001/new");
  });

  it("falls back to the app's name as its label on a legacy row", () => {
    expect(labelForApp({ name: "docs", label: "" })).toBe("docs");
    expect(labelForApp(app)).toBe("docs-x7k9q2w1");
  });
});
