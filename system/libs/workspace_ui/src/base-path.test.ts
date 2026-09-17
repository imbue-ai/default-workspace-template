// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

type BasePathModule = typeof import("./base-path");

/** A fresh module over one injected base path: the module caches the meta tag on first read. */
async function loadWithBasePath(basePath: string): Promise<BasePathModule> {
  vi.resetModules();
  document.head.innerHTML = `<meta name="system-interface-base-path" content="${basePath}">`;
  return await import("./base-path");
}

afterEach(() => {
  document.head.innerHTML = "";
});

describe("wsUrl", () => {
  it("rewrites an absolute http(s) base path to ws(s)", async () => {
    const { wsUrl } = await loadWithBasePath("https://workspace.example.test/app");
    expect(wsUrl("/api/ws", { protocol: "http:", host: "ignored" })).toBe("wss://workspace.example.test/app/api/ws");
  });

  it("uses the page's host with ws: under http:", async () => {
    const { wsUrl } = await loadWithBasePath("");
    expect(wsUrl("/api/ws", { protocol: "http:", host: "localhost:8010" })).toBe("ws://localhost:8010/api/ws");
  });

  it("uses the page's host with wss: under https:, keeping a relative base path", async () => {
    const { wsUrl } = await loadWithBasePath("/myapp/");
    expect(wsUrl("/api/ws", { protocol: "https:", host: "workspace.example.test" })).toBe(
      "wss://workspace.example.test/myapp/api/ws",
    );
  });
});
