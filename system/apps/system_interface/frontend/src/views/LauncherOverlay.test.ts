// @vitest-environment jsdom
import "../testing/dom";
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
    expect(searchTiles(launchTilesOf([docs, notes]), "open new no").map((tile) => tile.app.name)).toEqual(["notes"]);
    expect(searchTiles(launchTilesOf([docs, notes]), "new docs").map((tile) => tile.app.name)).toEqual(["docs"]);
  });

  it("seeds a chat's first message for adopting a template or making a machine from it", () => {
    const template = catalogTemplateRecord("inbox-digest");
    expect(adoptTemplateMessage(template)).toBe("/use-template https://github.com/someone/inbox-digest");
    expect(createMachineFromTemplateMessage(template)).toContain("https://github.com/someone/inbox-digest");
  });
});

let root: HTMLElement | null = null;

afterEach(() => {
  if (root !== null) {
    m.mount(root, null);
    root.remove();
    root = null;
  }
});

function render(overrides: Partial<LauncherOverlayAttrs> = {}): HTMLElement {
  root = document.createElement("div");
  document.body.appendChild(root);
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
  m.mount(root, { view: () => m(LauncherOverlay, attrs) });
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
    expect(
      [...overlay.querySelectorAll("[data-launcher-window]")].map((row) => row.getAttribute("data-launcher-window")),
    ).toEqual(["win-3"]);
    expect(overlay.querySelector('[data-launcher-window="win-3"]')?.textContent).toContain("Work");
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

  it("stands the intents down when no launch path takes a message", () => {
    const onRunLaunch = vi.fn();
    const overlay = render({ apps: [notes], onRunLaunch });
    const tile = overlay.querySelector('[data-start="build-app"]') as HTMLElement;
    expect(tile.getAttribute("aria-disabled")).toBe("true");
    tile.click();
    expect(onRunLaunch).not.toHaveBeenCalled();
  });
});
