// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import type { TemplateCatalogState } from "getting-started-frontend/src/models/TemplateCatalog";
import { catalogTemplateRecord } from "getting-started-frontend/src/testing/records";
import type { StoreState } from "../models/Store";
import { resetStoreStateForTests } from "../models/Store";
import { pendingRepoAddRecord, snapshotRecord, storeAppRecord, workspaceAppRecord } from "../testing/records";
import type { StorePageAttrs } from "./StorePage";
import { StorePage } from "./StorePage";

let root: HTMLElement;

beforeEach(() => {
  root = document.createElement("div");
  document.body.appendChild(root);
});

afterEach(() => {
  m.mount(root, null);
  root.remove();
  vi.unstubAllGlobals();
  resetStoreStateForTests();
});

const LOADED: StoreState = {
  kind: "loaded",
  snapshot: snapshotRecord({
    room: { disk_free_mb: 31_979, memory_available_mb: 3_710 },
    apps: [
      storeAppRecord("datasette", { status: "INSTALLED", measured_disk_mb: 21, app_url: "http://localhost:8080" }),
      storeAppRecord("marimo"),
      storeAppRecord("radicale", { description: "Calendars and contacts.", tags: ["automation"] }),
    ],
    workspace_apps: [
      workspaceAppRecord("chat", { display_name: "Chat" }),
      workspaceAppRecord("datasette", { display_name: "Datasette", origin: "STORE" }),
    ],
  }),
};

const TEMPLATES: TemplateCatalogState = {
  kind: "loaded",
  catalog: { generated_at: "", templates: [catalogTemplateRecord("digest")], shelves: [] },
  isStale: false,
};

function mountPage(overrides: Partial<StorePageAttrs>): StorePageAttrs {
  const attrs: StorePageAttrs = {
    store: LOADED,
    templates: TEMPLATES,
    onOpenLink: vi.fn(),
    onStartWithText: vi.fn(),
    ...overrides,
  };
  m.mount(root, { view: () => m(StorePage, attrs) });
  return attrs;
}

function typeInSearch(text: string): void {
  const field = root.querySelector<HTMLInputElement>("[data-app-store-search]")!;
  field.value = text;
  field.dispatchEvent(new Event("input"));
  m.redraw.sync();
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  m.redraw.sync();
}

describe("StorePage", () => {
  it("shows the room, this workspace's apps, then the apps still to install", () => {
    mountPage({});
    expect(root.querySelector("[data-stat=disk]")!.textContent).toBe("31.2 GBDisk free");
    expect(root.querySelector("[data-stat=memory]")!.textContent).toBe("3.6 GBMemory available");
    expect(root.querySelector("[data-stat=installed]")!.textContent).toBe("1Installed from the store");
    const installed = root.querySelector("[data-section=installed]")!;
    expect(
      Array.from(installed.querySelectorAll("[data-workspace-app]")).map((tile) =>
        tile.getAttribute("data-workspace-app"),
      ),
    ).toEqual(["chat", "datasette"]);
    const available = root.querySelector("[data-section=available]")!;
    expect(Array.from(available.querySelectorAll("[data-app]")).map((tile) => tile.getAttribute("data-app"))).toEqual([
      "marimo",
      "radicale",
    ]);
  });

  it("filters by category, keeping only store apps in it on this workspace", () => {
    mountPage({});
    const filters = Array.from(root.querySelectorAll<HTMLElement>("[data-category]"));
    expect(filters.map((filter) => filter.textContent)).toEqual(["All apps", "Data and notebooks", "Automation"]);
    expect(root.querySelector("[data-category=all]")!.getAttribute("aria-pressed")).toBe("true");

    root.querySelector<HTMLElement>("[data-category=automation]")!.click();
    m.redraw.sync();
    expect(root.querySelector("[data-category=automation]")!.getAttribute("aria-pressed")).toBe("true");
    expect(root.querySelector("[data-section=installed]")).toBeNull();
    expect(Array.from(root.querySelectorAll("[data-app]")).map((tile) => tile.getAttribute("data-app"))).toEqual([
      "radicale",
    ]);

    root.querySelector<HTMLElement>("[data-category=data]")!.click();
    m.redraw.sync();
    expect(
      Array.from(root.querySelectorAll("[data-workspace-app]")).map((tile) => tile.getAttribute("data-workspace-app")),
    ).toEqual(["datasette"]);
    expect(Array.from(root.querySelectorAll("[data-app]")).map((tile) => tile.getAttribute("data-app"))).toEqual([
      "marimo",
    ]);
    typeInSearch("zzz");
    expect(root.querySelector("[data-no-matches]")!.textContent).toBe("No apps match “zzz” in Data and notebooks.");

    root.querySelector<HTMLElement>("[data-category=all]")!.click();
    typeInSearch("");
    expect(root.querySelectorAll("[data-workspace-app]")).toHaveLength(2);
  });

  it("says when a category has no apps", () => {
    mountPage({
      store: {
        kind: "loaded",
        snapshot: snapshotRecord({ apps: [storeAppRecord("marimo")] }),
      },
    });
    root.querySelector<HTMLElement>("[data-category=automation]")!.click();
    m.redraw.sync();
    expect(root.querySelector("[data-no-matches]")!.textContent).toBe("No apps in Automation yet.");
  });

  it("opens the Add app dialog from the header", () => {
    mountPage({});
    root.querySelector<HTMLElement>(".app-store-add-app")!.click();
    m.redraw.sync();
    expect(document.querySelector("[data-dialog=add-app]")).not.toBeNull();
  });

  it("asks before removing an added app from the store", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ slug: "demo" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    mountPage({
      store: { kind: "loaded", snapshot: snapshotRecord({ apps: [storeAppRecord("demo", { is_added: true })] }) },
    });
    expect(root.querySelector("[data-app=demo]")!.textContent).toContain("Added by you");
    root.querySelector<HTMLElement>("[data-app=demo]")!.click();
    m.redraw.sync();
    root.querySelector<HTMLElement>(".app-store-remove")!.click();
    m.redraw.sync();
    document.querySelector<HTMLElement>(".app-store-confirm-remove")!.click();
    await settle();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/added-apps/demo");
    expect(root.querySelector("[data-app-detail]")).toBeNull();
  });

  it("says it is loading, or that the store failed to load", () => {
    mountPage({ store: { kind: "loading" } });
    expect(root.querySelector("[data-store-status]")!.textContent).toBe("Loading…");
    m.mount(root, null);
    mountPage({ store: { kind: "failed" } });
    expect(root.querySelector("[data-store-status]")!.textContent).toBe("Failed to load the store.");
  });

  it("filters both lists by the search, and says when nothing matches", () => {
    mountPage({});
    typeInSearch("calendars");
    expect(root.querySelector("[data-section=installed]")).toBeNull();
    expect(Array.from(root.querySelectorAll("[data-app]")).map((tile) => tile.getAttribute("data-app"))).toEqual([
      "radicale",
    ]);
    typeInSearch("zzz");
    expect(root.querySelector("[data-no-matches]")!.textContent).toBe("No apps match “zzz”.");
  });

  it("opens an app through the shell", () => {
    const attrs = mountPage({});
    root.querySelector<HTMLElement>("[data-workspace-app=chat] .app-store-open")!.click();
    expect(attrs.onOpenLink).toHaveBeenCalledWith("http://localhost:9400");
  });

  it("asks before installing, queues the install, and closes the dialog", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ record: {} }), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    mountPage({});
    root.querySelector<HTMLElement>("[data-app=marimo] .app-store-install")!.click();
    m.redraw.sync();
    expect(document.querySelector("[data-dialog=install]")).not.toBeNull();
    document.querySelector<HTMLElement>(".app-store-confirm-install")!.click();
    await settle();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/apps/marimo/install");
    expect(document.querySelector("[data-dialog=install]")).toBeNull();
  });

  it("keeps the dialog open with the store's reason when it refuses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Marimo won't fit." }), { status: 409 })),
    );
    mountPage({});
    root.querySelector<HTMLElement>("[data-app=marimo] .app-store-install")!.click();
    m.redraw.sync();
    document.querySelector<HTMLElement>(".app-store-confirm-install")!.click();
    await settle();
    expect(document.querySelector("[data-refusal]")!.textContent).toBe("Marimo won't fit.");
  });

  it("asks before uninstalling an app the store installed", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ record: {} }), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    mountPage({});
    root.querySelector<HTMLElement>("[data-workspace-app=datasette]")!.click();
    m.redraw.sync();
    root.querySelector<HTMLElement>("[data-app-detail=datasette] .app-store-uninstall")!.click();
    m.redraw.sync();
    document.querySelector<HTMLElement>(".app-store-confirm-uninstall")!.click();
    await settle();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/apps/datasette/uninstall");
  });

  it("shows a store app's page and goes back", () => {
    mountPage({});
    root.querySelector<HTMLElement>("[data-app=marimo]")!.click();
    m.redraw.sync();
    expect(root.querySelector("[data-app-detail=marimo]")).not.toBeNull();
    root.querySelector<HTMLElement>(".app-store-back")!.click();
    m.redraw.sync();
    expect(root.querySelector("[data-section=available]")).not.toBeNull();
  });

  it("switches to the templates and shows one's page with its size", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ size_kb: 1024 }), { status: 200 })),
    );
    const attrs = mountPage({});
    root.querySelector<HTMLElement>("[data-segment=templates]")!.click();
    m.redraw.sync();
    root.querySelector<HTMLElement>("[data-template=digest]")!.click();
    m.redraw.sync();
    expect(root.querySelector("[data-template-size]")!.textContent).toBe("Estimating…");
    await settle();
    expect(root.querySelector("[data-template-size]")!.textContent).toBe("about 1 MB");
    root.querySelector<HTMLElement>(".app-store-adopt")!.click();
    expect(attrs.onStartWithText).toHaveBeenCalledWith("/use-template https://github.com/someone/digest");
  });

  it("searches the templates, and says when the catalog cannot be shown", () => {
    mountPage({});
    root.querySelector<HTMLElement>("[data-segment=templates]")!.click();
    m.redraw.sync();
    typeInSearch("dig");
    expect(root.querySelectorAll("[data-template=digest]")).toHaveLength(1);
    typeInSearch("zzz");
    expect(root.querySelector("[data-no-matches]")!.textContent).toBe("No templates match “zzz”.");
    m.mount(root, null);
    mountPage({ templates: { kind: "failed" } });
    root.querySelector<HTMLElement>("[data-segment=templates]")!.click();
    m.redraw.sync();
    expect(root.textContent).toContain("Failed to load templates.");
  });
});

describe("apps a chat worked out how to install from GitHub", () => {
  const WAITING: StoreState = {
    kind: "loaded",
    snapshot: snapshotRecord({
      apps: [storeAppRecord("marimo")],
      pending_repo_adds: [pendingRepoAddRecord("first"), pendingRepoAddRecord("second", { title: "Other App" })],
    }),
  };

  it("asks about the oldest one, and the next after Not now", () => {
    mountPage({ store: WAITING });
    expect(root.querySelector("[data-dialog=repo-add]")!.textContent).toContain("Add Datasette Latest to the store?");
    [...root.querySelectorAll<HTMLElement>("button")].find((button) => button.textContent === "Not now")!.click();
    m.redraw.sync();
    expect(root.querySelector("[data-dialog=repo-add]")!.textContent).toContain("Add Other App to the store?");
  });

  it("confirms it with the store", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    mountPage({ store: WAITING });
    root.querySelector<HTMLElement>(".app-store-confirm-repo-add")!.click();
    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/api/repo-adds/first/confirm"))).toBe(true),
    );
  });
});
