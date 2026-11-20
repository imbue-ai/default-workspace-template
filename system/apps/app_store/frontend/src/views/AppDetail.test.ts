// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { storeAppRecord } from "../testing/records";
import type { NewestVersionAnswer } from "../models/Store";
import type { AppDetailAttrs } from "./AppDetail";
import { AppDetail } from "./AppDetail";

let root: HTMLElement;

beforeEach(() => {
  root = document.createElement("div");
  document.body.appendChild(root);
});

afterEach(() => {
  m.mount(root, null);
  root.remove();
});

function mountDetail(overrides: Partial<AppDetailAttrs>): AppDetailAttrs {
  const attrs: AppDetailAttrs = {
    app: storeAppRecord("datasette"),
    room: { disk_free_mb: 1000, memory_available_mb: 100 },
    onBack: vi.fn(),
    onRemove: vi.fn(),
    onPick: vi.fn(),
    onInstall: vi.fn(),
    onOpen: vi.fn(),
    onUninstall: vi.fn(),
    onUpdate: vi.fn(),
    onStartWithText: vi.fn(),
    ...overrides,
  };
  m.mount(root, { view: () => m(AppDetail, attrs) });
  return attrs;
}

describe("AppDetail", () => {
  it("shows the app's icon on its own, with no frame around it", () => {
    mountDetail({});
    const holder = root.querySelector<HTMLElement>("[data-detail-icon]")!;
    expect(holder.className).toBe("shrink-0");
    expect(holder.querySelector("img")!.getAttribute("src")).toBe("/api/apps/datasette/icon.svg");
  });

  it("measures the app against the room and says what it installs and where its files go", () => {
    mountDetail({ app: storeAppRecord("datasette", { verdict: "TIGHT" }) });
    const meters = root.querySelectorAll<HTMLElement>("[data-meter] > div");
    expect(Array.from(meters).map((meter) => meter.style.width)).toEqual(["10%", "50%"]);
    expect(root.querySelector("[data-room-note]")!.textContent).toBe(
      "It fits, but leaves little room for anything else.",
    );
    expect(root.querySelector("[data-memory-idle]")!.textContent).toBe(
      "About 30 MB when nobody is using it. The fit is checked against the in-use figure.",
    );
    expect(root.textContent).toContain("Python package datasette==1.0.0");
    expect(root.textContent).toContain("App Store › datasette › data, in Files");
    const links = Array.from(root.querySelectorAll("a")).map((link) => link.getAttribute("href"));
    expect(links).toEqual(["https://example.test/datasette", "https://github.com/someone/datasette"]);
  });

  it("disables Install for an app that won't fit, painting the meter it overflows", () => {
    mountDetail({
      app: storeAppRecord("nodered", { memory_in_use_mb: 220, verdict: "WONT_FIT", blocked_reason: "Too big." }),
    });
    expect(root.querySelector<HTMLButtonElement>(".app-store-install")!.disabled).toBe(true);
    expect(root.querySelector("[data-room-note]")!.textContent).toBe("Too big.");
    expect(root.querySelectorAll(".bg-danger")).toHaveLength(1);
  });

  it("opens and uninstalls an installed app, and links its install log", () => {
    const app = storeAppRecord("datasette", {
      status: "INSTALLED",
      app_url: "http://localhost:8080",
      measured_disk_mb: 21,
      has_log: true,
    });
    const attrs = mountDetail({ app });
    expect(root.querySelector("[data-installed-note]")!.textContent).toBe(
      "Installed, it takes 21 MB of disk; it isn't running right now (it stops a minute after its window closes).",
    );
    root.querySelector<HTMLElement>(".app-store-open")!.click();
    expect(attrs.onOpen).toHaveBeenCalledWith("http://localhost:8080");
    root.querySelector<HTMLElement>(".app-store-uninstall")!.click();
    expect(attrs.onUninstall).toHaveBeenCalledWith(app);
    expect(Array.from(root.querySelectorAll("a")).map((link) => link.textContent)).toContain("Install log");
  });

  it("says what a running installed app uses right now", () => {
    mountDetail({ app: storeAppRecord("datasette", { status: "INSTALLED", memory_now_mb: 242 }) });
    expect(root.querySelector("[data-installed-note]")!.textContent).toBe(
      "Installed; it is using 242 MB of memory right now.",
    );
  });

  it("shows what an added app was added with, its unknown figures, and offers Remove only once uninstalled", () => {
    const added = storeAppRecord("demo", {
      is_added: true,
      disk_mb: null,
      memory_idle_mb: null,
      memory_in_use_mb: null,
      resources_basis: "Size unknown: resolving failed.",
      added_with: {
        registry: "PyPI",
        package: "demo",
        version: "1.1",
        command: ["demo", "--port", "{port}", "127.0.0.1"],
        python: "3.12",
        registry_url: "https://pypi.org/project/demo/1.1/",
        package_count: 2,
        chat_id: "",
      },
    });
    const attrs = mountDetail({ app: added });
    const section = root.querySelector("[data-added-with]")!;
    expect(section.textContent).toContain("PyPI: demo");
    expect(section.textContent).toContain("1.1 (repairs and reinstalls keep it)");
    expect(section.textContent).toContain("demo --port {port} 127.0.0.1");
    expect(section.querySelector("a")!.getAttribute("href")).toBe("https://pypi.org/project/demo/1.1/");
    expect(root.querySelector("[data-disk-unknown]")!.textContent).toContain("unknown");
    expect(root.querySelector("[data-memory-unknown]")!.textContent).toContain("unknown until it runs");
    expect(root.querySelector("[data-basis]")!.textContent).toContain("Size unknown: resolving failed.");
    root.querySelector<HTMLElement>(".app-store-remove")!.click();
    expect(attrs.onRemove).toHaveBeenCalledWith(added);

    m.mount(root, null);
    mountDetail({ app: { ...added, status: "INSTALLED", peak_memory_mb: 312 } });
    expect(root.querySelector<HTMLButtonElement>(".app-store-remove")!.disabled).toBe(true);
    expect(root.textContent).toContain("Uninstall it first");
    expect(root.textContent).toContain("Memory (measured peak)");
  });

  it("shows a running install's progress instead of buttons, and goes back", () => {
    const attrs = mountDetail({ app: storeAppRecord("datasette", { status: "DOWNLOADING" }) });
    expect(root.querySelector("[data-install-stage=DOWNLOADING]")).not.toBeNull();
    expect(root.querySelector(".app-store-install")).toBeNull();
    root.querySelector<HTMLElement>(".app-store-back")!.click();
    expect(attrs.onBack).toHaveBeenCalledTimes(1);
  });

  it("shows why the last install failed and offers to try again", () => {
    const attrs = mountDetail({ app: storeAppRecord("datasette", { status: "FAILED", failure_reason: "Broke." }) });
    expect(root.querySelector("[data-failure]")!.textContent).toBe("Broke.");
    const retry = root.querySelector<HTMLElement>(".app-store-install")!;
    expect(retry.textContent).toBe("Try again");
    retry.click();
    expect(attrs.onInstall).toHaveBeenCalledTimes(1);
  });
});

describe("updates on an installed app's page", () => {
  const installed = { status: "INSTALLED" as const, app_url: "http://localhost:8080", installed_version: "1.0" };

  it("offers the version the workspace brought for a shipped app", () => {
    const attrs = mountDetail({
      app: storeAppRecord("datasette", { ...installed, update_kind: "CATALOG", update_version: "1.2" }),
    });
    expect(root.querySelector("[data-installed-version]")!.textContent).toBe("1.0 installed");
    expect(root.querySelector("[data-update-status]")!.textContent).toBe("Version 1.2 is available.");
    root.querySelector<HTMLElement>(".app-store-update")!.click();
    expect(attrs.onUpdate).toHaveBeenCalledWith(attrs.app, "1.2");
  });

  it("checks PyPI or npm for an added package and offers what it found", async () => {
    let answer: NewestVersionAnswer = {
      kind: "checked",
      installedVersion: "1.0",
      newestVersion: "1.1",
      isNewer: true,
    };
    const checkNewest = vi.fn(async () => answer);
    const attrs = mountDetail({
      app: storeAppRecord("demo", { ...installed, update_kind: "REGISTRY" }),
      checkNewest,
    });
    expect(root.querySelector(".app-store-update")).toBeNull();
    root.querySelector<HTMLElement>(".app-store-check-update")!.click();
    await vi.waitFor(() => expect(root.querySelector(".app-store-update")).not.toBeNull());
    m.redraw.sync();
    expect(root.querySelector("[data-update-status]")!.textContent).toBe("Version 1.1 is available.");
    root.querySelector<HTMLElement>(".app-store-update")!.click();
    expect(attrs.onUpdate).toHaveBeenCalledWith(attrs.app, "1.1");

    answer = { kind: "refused", reason: "PyPI couldn't be reached." };
    m.mount(root, null);
    mountDetail({ app: storeAppRecord("demo", { ...installed, update_kind: "REGISTRY" }), checkNewest });
    root.querySelector<HTMLElement>(".app-store-check-update")!.click();
    await vi.waitFor(() =>
      expect(root.querySelector("[data-update-status]")!.textContent).toBe("PyPI couldn't be reached."),
    );
  });

  it("forgets what a check found once the app is on another version", async () => {
    const checkNewest = vi.fn(async (): Promise<NewestVersionAnswer> => ({
      kind: "checked",
      installedVersion: "1.0",
      newestVersion: "1.1",
      isNewer: true,
    }));
    const attrs = mountDetail({ app: storeAppRecord("demo", { ...installed, update_kind: "REGISTRY" }), checkNewest });
    root.querySelector<HTMLElement>(".app-store-check-update")!.click();
    await vi.waitFor(() => expect(root.querySelector(".app-store-update")).not.toBeNull());

    attrs.app = storeAppRecord("demo", { ...installed, installed_version: "1.1", update_kind: "REGISTRY" });
    m.redraw.sync();
    expect(root.querySelector("[data-installed-version]")!.textContent).toBe("1.1 installed");
    expect(root.querySelector(".app-store-update")).toBeNull();
    expect(root.querySelector(".app-store-check-update")).not.toBeNull();
  });

  it("opens a chat to look for a newer version of an app added from GitHub", () => {
    const app = storeAppRecord("repoapp", {
      ...installed,
      update_kind: "GITHUB",
      homepage_url: "https://github.com/someone/repo-app",
    });
    const attrs = mountDetail({ app });
    root.querySelector<HTMLElement>(".app-store-github-update")!.click();
    expect(attrs.onStartWithText).toHaveBeenCalledWith(
      "/add-repo-to-store https://github.com/someone/repo-app --update repoapp",
    );
  });

  it("says when an update didn't work and the old version was put back", () => {
    mountDetail({
      app: storeAppRecord("datasette", {
        ...installed,
        update_kind: "CATALOG",
        failure_reason: "Updating to 1.2 didn't work, so 1.0 was put back: it didn't start.",
      }),
    });
    expect(root.querySelector("[data-update-failure]")!.textContent).toContain("1.0 was put back");
  });

  it("shows nothing about versions for an app that isn't installed", () => {
    mountDetail({ app: storeAppRecord("datasette") });
    expect(root.querySelector("[data-updates]")).toBeNull();
  });
});
