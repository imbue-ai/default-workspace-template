// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "../testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { windowTitle } from "../reducers/desktopState";
import { appRecord, catalogTemplateRecord, desktopRecord, launchPathRecord, windowRecord } from "../testing/records";
import {
  LauncherOverlay,
  adoptTemplateMessage,
  createMachineFromTemplateMessage,
  searchTiles,
  searchWindowRows,
  windowRowsOf,
} from "./LauncherOverlay";
import type { LauncherOverlayAttrs, LauncherWindowRow } from "./LauncherOverlay";
import { launchTilesOf } from "../model/launch";

const docs = appRecord("docs", {
  launcher_rank: 20,
  launch_paths: [launchPathRecord({ id: "new", label: "New docs", params: ["message"] })],
});
const notes = appRecord("notes", {
  launcher_rank: 10,
  launch_paths: [launchPathRecord({ id: "new", label: "New notes" })],
});
const home = desktopRecord("home", {
  windows: [windowRecord("win-1", "docs", "/a", { title: "Plan" }), windowRecord("win-2", "notes", "/b")],
});
const work = desktopRecord("work", { windows: [windowRecord("win-3", "docs", "/c", { title: "Budget" })] });

function rows(activeDesktopId: string): LauncherWindowRow[] {
  return windowRowsOf(
    [home, work],
    activeDesktopId,
    (name) => [docs, notes].find((app) => app.name === name),
    windowTitle,
    (_desktopId, windowId) => windowId === "win-2",
  );
}

describe("the launcher's pure helpers", () => {
  it("lists every desktop's windows, the active desktop's first, titled after their page or their app", () => {
    expect(rows("work").map((row) => [row.window.id, row.desktopId, row.title, row.isMinimized])).toEqual([
      ["win-3", "work", "Budget", false],
      ["win-1", "home", "Plan", false],
      ["win-2", "home", "Notes", true],
    ]);
  });

  it("searches windows by title, desktop, and app, and tiles by their row text and labels", () => {
    expect(searchWindowRows(rows("home"), "budget").map((row) => row.window.id)).toEqual(["win-3"]);
    expect(searchWindowRows(rows("home"), "notes").map((row) => row.window.id)).toEqual(["win-2"]);
    expect(searchWindowRows(rows("home"), "work").map((row) => row.window.id)).toEqual(["win-3"]);
    // The raw path is not what the user reads: a fragment of one finds nothing.
    expect(searchWindowRows(rows("home"), "/c")).toEqual([]);
    expect(searchTiles(launchTilesOf([docs, notes]), "open new no").map((tile) => tile.app.name)).toEqual(["notes"]);
    expect(searchTiles(launchTilesOf([docs, notes]), "new docs").map((tile) => tile.app.name)).toEqual(["docs"]);
  });

  it("seeds a chat's first message for adopting a template or making a machine from it", () => {
    const template = catalogTemplateRecord("inbox-digest");
    expect(adoptTemplateMessage(template)).toBe("/use-template https://github.com/someone/inbox-digest");
    expect(createMachineFromTemplateMessage(template)).toContain("https://github.com/someone/inbox-digest");
  });
});

// jsdom has no scrollIntoView; the scroll test stands one in and puts this back.
const originalScrollIntoView = Element.prototype.scrollIntoView;

afterEach(() => {
  unmountViews();
  Element.prototype.scrollIntoView = originalScrollIntoView;
});

function render(overrides: Partial<LauncherOverlayAttrs> = {}): HTMLElement {
  const attrs: LauncherOverlayAttrs = {
    query: "",
    apps: [docs, notes, appRecord("hidden", { internal: true })],
    windows: rows("home"),
    activeDesktopId: "home",
    catalog: { kind: "disabled" },
    isCompact: false,
    onRunLaunch: vi.fn(),
    onPickWindow: vi.fn(),
    ...overrides,
  };
  const root = mountView(() => m(LauncherOverlay, attrs));
  return root.querySelector("[data-launcher-overlay]") as HTMLElement;
}

describe("LauncherOverlay", () => {
  it("rests on the tiles (ranked apps first), this desktop's windows, and the intents", () => {
    const onRunLaunch = vi.fn();
    const onPickWindow = vi.fn();
    const overlay = render({ onRunLaunch, onPickWindow });
    expect([...overlay.querySelectorAll(".launcher-tile")].map((tile) => tile.getAttribute("data-launch"))).toEqual([
      "notes:new",
      "docs:new",
    ]);
    expect(
      [...overlay.querySelectorAll("[data-launcher-window]")].map((row) => row.getAttribute("data-launcher-window")),
    ).toEqual(["win-1", "win-2"]);
    expect(overlay.querySelector('[data-launcher-window="win-2"]')?.getAttribute("data-minimized")).toBe("true");
    expect(overlay.querySelectorAll("[data-start]")).toHaveLength(6);
    expect(overlay.querySelector('[data-section="templates"]')).toBeNull();

    (overlay.querySelector('[data-launch="docs:new"]') as HTMLElement).click();
    expect(onRunLaunch).toHaveBeenCalledWith(docs, docs.launch_paths[0], {});
    (overlay.querySelector('[data-launcher-window="win-1"]') as HTMLElement).click();
    expect(onPickWindow).toHaveBeenCalledWith(expect.objectContaining({ window: home.windows[0] }));
  });

  it("swaps the sections for results while searching, windows across every desktop included", () => {
    const overlay = render({ query: "budget" });
    expect(overlay.querySelectorAll(".launcher-tile")).toHaveLength(0);
    expect(overlay.querySelector('[data-section="open-new"]')).toBeNull();
    expect(
      [...overlay.querySelectorAll("[data-launcher-window]")].map((row) => row.getAttribute("data-launcher-window")),
    ).toEqual(["win-3"]);
    expect(overlay.querySelector('[data-launcher-window="win-3"]')?.textContent).toContain("Work");
  });

  it("files a matching launch path under Open new, apart from the windows it also finds", () => {
    const overlay = render({ query: "docs" });
    const openNew = overlay.querySelector('[data-section="open-new"]');
    expect(openNew?.querySelector('[data-launch="docs:new"]')).not.toBeNull();
    expect(openNew?.querySelector("[data-launcher-window]")).toBeNull();
    expect(overlay.querySelector('[data-section="windows"] [data-launch]')).toBeNull();
    expect(overlay.querySelector('[data-section="windows"] [data-launcher-window]')).not.toBeNull();
  });

  it("says when nothing matches", () => {
    const overlay = render({ query: "zzzzzz" });
    expect(overlay.querySelector(".launcher-no-matches")?.textContent).toContain("zzzzzz");
  });

  it("sends a Start something intent to the launch path that takes a message", () => {
    const onRunLaunch = vi.fn();
    const overlay = render({ onRunLaunch });
    (overlay.querySelector('[data-start="build-app"]') as HTMLElement).click();
    expect(onRunLaunch).toHaveBeenCalledWith(docs, docs.launch_paths[0], {
      message: expect.stringContaining("build a new app"),
    });
  });

  it("the template tile scrolls to the templates on hand, and owes the resting page nothing when there are none", () => {
    const scrolled: string[] = [];
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this.getAttribute("data-section") ?? "");
    };
    const catalog = {
      kind: "loaded" as const,
      isStale: false,
      catalog: {
        generated_at: "",
        templates: [catalogTemplateRecord("inbox-digest", { title: "Inbox digest template" })],
        shelves: [{ key: "all", title: "All", slugs: ["inbox-digest"] }],
      },
    };
    let query = "start from a";
    const root = mountView(() =>
      m(LauncherOverlay, {
        query,
        apps: [docs, notes],
        windows: [],
        activeDesktopId: "home",
        catalog,
        isCompact: false,
        onRunLaunch: vi.fn(),
        onPickWindow: vi.fn(),
      }),
    );
    // These results hold the tile and no template: the pick has nowhere to scroll, now or later.
    expect(root.querySelector('[data-section="templates"]')).toBeNull();
    (root.querySelector('[data-start="template"]') as HTMLElement).click();
    m.redraw.sync();
    query = "";
    m.redraw.sync();
    expect(root.querySelector('[data-section="templates"]')).not.toBeNull();
    expect(scrolled).toEqual([]);
    // These results hold both: the tile scrolls the results' own templates section.
    query = "template";
    m.redraw.sync();
    (root.querySelector('[data-start="template"]') as HTMLElement).click();
    m.redraw.sync();
    expect(scrolled).toEqual(["templates"]);
  });

  it("stands the intents down when no launch path takes a message", () => {
    const onRunLaunch = vi.fn();
    const overlay = render({ apps: [notes], onRunLaunch });
    const tile = overlay.querySelector('[data-start="build-app"]') as HTMLElement;
    expect(tile.getAttribute("aria-disabled")).toBe("true");
    tile.click();
    expect(onRunLaunch).not.toHaveBeenCalled();
  });
});
