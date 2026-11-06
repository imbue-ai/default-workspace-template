// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { storeAppRecord, workspaceAppRecord } from "../testing/records";
import type { StoreAppTileAttrs, WorkspaceAppTileAttrs } from "./AppTile";
import { StoreAppTile, WorkspaceAppTile } from "./AppTile";

let root: HTMLElement;

beforeEach(() => {
  root = document.createElement("div");
  document.body.appendChild(root);
});

afterEach(() => {
  m.mount(root, null);
  root.remove();
});

function actions(): Pick<StoreAppTileAttrs, "onPick" | "onInstall" | "onOpen" | "onUninstall"> {
  return { onPick: vi.fn(), onInstall: vi.fn(), onOpen: vi.fn(), onUninstall: vi.fn() };
}

function mountStoreTile(overrides: Partial<StoreAppTileAttrs>): StoreAppTileAttrs {
  const attrs: StoreAppTileAttrs = {
    app: storeAppRecord("datasette"),
    busyWithTitle: null,
    ...actions(),
    ...overrides,
  };
  m.mount(root, { view: () => m(StoreAppTile, attrs) });
  return attrs;
}

function text(selector: string): string {
  return root.querySelector(selector)?.textContent ?? "";
}

describe("StoreAppTile", () => {
  it("shows an available app's cost and room, and installs it without opening its page", () => {
    const attrs = mountStoreTile({});
    expect(text("[data-cost]")).toBe("Disk 100 MBMemory 50 MB in useFits");
    expect(root.querySelector("img")!.getAttribute("src")).toBe("/api/apps/datasette/icon.svg");
    root.querySelector<HTMLElement>(".app-store-install")!.click();
    expect(attrs.onInstall).toHaveBeenCalledWith(attrs.app);
    expect(attrs.onPick).not.toHaveBeenCalled();
    root.querySelector<HTMLElement>("[data-app=datasette]")!.click();
    expect(attrs.onPick).toHaveBeenCalledWith(attrs.app);
  });

  it("says which install it waits behind", () => {
    mountStoreTile({ busyWithTitle: "JupyterLab" });
    expect(root.textContent).toContain("Starts after JupyterLab finishes");
  });

  it("blocks an app that won't fit with the store's reason and no Install", () => {
    mountStoreTile({
      app: storeAppRecord("nodered", {
        verdict: "WONT_FIT",
        blocked_reason: "Node-RED needs about 220 MB of memory.",
      }),
    });
    expect(text("[data-blocked]")).toBe("Node-RED needs about 220 MB of memory.");
    expect(root.querySelector(".app-store-install")).toBeNull();
  });

  it("shows an install's stage and links its raw log while it runs", () => {
    mountStoreTile({ app: storeAppRecord("datasette", { status: "CONFIGURING", has_log: true }) });
    expect(root.querySelector("[data-install-stage]")!.getAttribute("data-install-stage")).toBe("CONFIGURING");
    expect(root.textContent).toContain("Setting up…");
    expect(root.querySelector("a")!.getAttribute("href")).toBe("/api/apps/datasette/log");
    expect(root.querySelector(".app-store-install")).toBeNull();
  });

  it("shows why an install failed, its log, and Try again", () => {
    const attrs = mountStoreTile({
      app: storeAppRecord("datasette", { status: "FAILED", failure_reason: "It didn't start.", has_log: true }),
    });
    expect(text("[data-failure]")).toBe("It didn't start.");
    root.querySelector<HTMLElement>(".app-store-retry")!.click();
    expect(attrs.onInstall).toHaveBeenCalledWith(attrs.app);
  });
});

describe("WorkspaceAppTile", () => {
  function mountWorkspaceTile(overrides: Partial<WorkspaceAppTileAttrs>): WorkspaceAppTileAttrs {
    const attrs: WorkspaceAppTileAttrs = {
      app: workspaceAppRecord("files"),
      storeApp: null,
      ...actions(),
      ...overrides,
    };
    m.mount(root, { view: () => m(WorkspaceAppTile, attrs) });
    return attrs;
  }

  it("opens a built-in app at its address, drawing its registered icon, with no Uninstall", () => {
    const attrs = mountWorkspaceTile({});
    expect(root.textContent).toContain("Built in");
    expect(root.querySelector("img")!.getAttribute("src")).toBe("data:image/svg+xml;charset=utf-8,%3Csvg%2F%3E");
    expect(root.querySelector(".app-store-uninstall")).toBeNull();
    root.querySelector<HTMLElement>(".app-store-open")!.click();
    expect(attrs.onOpen).toHaveBeenCalledWith("http://localhost:9500");
  });

  it("says what an app the store installed uses now, and opens its page (with Uninstall) on a click", () => {
    const storeApp = storeAppRecord("datasette", { status: "INSTALLED", measured_disk_mb: 21, memory_now_mb: 64 });
    const attrs = mountWorkspaceTile({ app: workspaceAppRecord("datasette", { origin: "STORE" }), storeApp });
    expect(root.textContent).toContain("App Store · using 64 MB");
    root.querySelector<HTMLElement>(".app-store-open")!.click();
    expect(attrs.onOpen).toHaveBeenCalledWith("http://localhost:9900");
    expect(attrs.onPick).not.toHaveBeenCalled();
    root.querySelector<HTMLElement>("[data-workspace-app=datasette]")!.click();
    expect(attrs.onPick).toHaveBeenCalledWith(storeApp);
  });

  it("shows an app being uninstalled without its buttons", () => {
    mountWorkspaceTile({
      app: workspaceAppRecord("datasette", { origin: "STORE" }),
      storeApp: storeAppRecord("datasette", { status: "UNINSTALLING" }),
    });
    expect(root.textContent).toContain("Uninstalling…");
    expect(root.querySelector(".app-store-open")).toBeNull();
  });

  it("says when an installed store app is not running", () => {
    mountWorkspaceTile({
      app: workspaceAppRecord("datasette", { origin: "STORE" }),
      storeApp: storeAppRecord("datasette", { status: "INSTALLED" }),
    });
    expect(root.textContent).toContain("App Store · not running");
  });

  it("labels an app made in this workspace and draws a blank tile for one with no icon", () => {
    mountWorkspaceTile({ app: workspaceAppRecord("tool", { origin: "MADE_HERE", icon_svg: "" }) });
    expect(root.textContent).toContain("Made in this workspace");
    expect(root.querySelector("img")).toBeNull();
  });
});
