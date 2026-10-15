import { describe, expect, it } from "vitest";
import { appRecord } from "../testing/records";
import { labelForApp, windowAtBackendUrl, windowPageUrl } from "./pageUrl";

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

describe("windowAtBackendUrl", () => {
  const apps = [
    appRecord("files", { url: "http://localhost:8300" }),
    appRecord("news", { url: "http://127.0.0.1:8095/" }),
  ];

  it("finds the app registered at the link's port, under any loopback host name, with the link's path", () => {
    expect(windowAtBackendUrl(apps, "http://localhost:8095/story/7?ref=chat#top")).toEqual({
      app: apps[1],
      path: "/story/7?ref=chat",
    });
    expect(windowAtBackendUrl(apps, "http://[::1]:8300/")?.app.name).toBe("files");
    expect(windowAtBackendUrl(apps, "http://127.0.0.1:8300")?.path).toBe("/");
  });

  it("finds nothing for a port no app is registered at, or another scheme", () => {
    expect(windowAtBackendUrl(apps, "http://localhost:3000/")).toBeNull();
    expect(windowAtBackendUrl(apps, "https://localhost:8095/")).toBeNull();
  });

  it("finds nothing for a host that is not a loopback name", () => {
    expect(windowAtBackendUrl(apps, "http://dev.localhost:8095/")).toBeNull();
    expect(windowAtBackendUrl(apps, "http://example.com:8095/")).toBeNull();
  });
});
