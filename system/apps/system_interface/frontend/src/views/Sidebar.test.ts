// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import type { AppRecord } from "../models/Inventory";
import { applyApps, resetInventoryForTesting } from "../models/Inventory";
import { EVERYTHING_VIEW_ID } from "../models/Projects";
import { Sidebar, effectiveShortcuts, nextGlyphIndex, nextProjectName, placeMenu, shortcutLabel } from "./Sidebar";
import type { SidebarAttrs, SidebarTabRow } from "./Sidebar";
import { SQUIGGLE_GLYPHS } from "./squiggles";
import { appRecord, instanceRecord, projectRecord } from "../testing/records";

function app(name: string, overrides: Partial<AppRecord> = {}): AppRecord {
  return appRecord(name, {
    instances: [instanceRecord({ key: "one", title: `${name} one` })],
    ...overrides,
  });
}

const project = projectRecord;

describe("placeMenu", () => {
  const viewport = { width: 1000, height: 800 };
  const size = { width: 200, height: 100 };

  it("hangs below the anchor and flips above when the bottom would overflow", () => {
    const anchor = { left: 50, right: 250, top: 100, bottom: 130, width: 200 };
    expect(placeMenu(anchor, size, viewport, "below")).toEqual({ left: 50, top: 130 });
    const low = { left: 50, right: 250, top: 760, bottom: 790, width: 200 };
    expect(placeMenu(low, size, viewport, "below")).toEqual({ left: 50, top: 660 });
  });

  it("sits beside the anchor and flips left when the right edge would overflow", () => {
    const anchor = { left: 50, right: 250, top: 100, bottom: 130, width: 200 };
    expect(placeMenu(anchor, size, viewport, "right")).toEqual({ left: 250, top: 100 });
    const farRight = { left: 850, right: 950, top: 100, bottom: 130, width: 100 };
    expect(placeMenu(farRight, size, viewport, "right")).toEqual({ left: 650, top: 100 });
  });

  it("clamps inside the window margin", () => {
    const anchor = { left: 0, right: 10, top: 0, bottom: 10, width: 10 };
    expect(placeMenu(anchor, size, viewport, "below")).toEqual({ left: 0, top: 10 });
    const tall = { width: 200, height: 2000 };
    expect(placeMenu(anchor, tall, viewport, "below").top).toBe(6);
  });
});

describe("shortcuts", () => {
  it("resolves a project's rail against the inventory, dropping what the machine no longer offers", () => {
    const apps = [app("chat"), app("terminal")];
    const alpha = project("alpha", {
      shortcuts: [
        { app: "terminal", action: "new", mode: "new" },
        { app: "gone", action: "new", mode: "focus" },
        { app: "chat", action: "missing", mode: "focus" },
      ],
    });
    const resolved = effectiveShortcuts(alpha, apps);
    expect(resolved.map((entry) => `${entry.app.name}:${entry.action.id}:${entry.mode}`)).toEqual([
      "terminal:new:new",
    ]);
    expect(shortcutLabel(resolved[0])).toBe("New terminal");
  });

  it("gives Everything every app's primary action in focus mode", () => {
    const resolved = effectiveShortcuts(null, [app("chat"), app("files", { actions: [] }), app("terminal")]);
    expect(resolved.map((entry) => `${entry.app.name}:${entry.mode}`)).toEqual(["chat:focus", "terminal:focus"]);
    expect(shortcutLabel(resolved[0])).toBe("Chat");
  });
});

describe("new projects", () => {
  it("picks the first free Project N by name or id", () => {
    expect(nextProjectName([])).toBe("Project 1");
    expect(
      nextProjectName([
        { id: "project-1", name: "Renamed" },
        { id: "x", name: " project 2 " },
      ]),
    ).toBe("Project 3");
  });

  it("picks the first unused glyph, then cycles", () => {
    expect(nextGlyphIndex([0, 1])).toBe(2);
    const all = SQUIGGLE_GLYPHS.map((_, index) => index);
    expect(nextGlyphIndex(all)).toBe(0);
    expect(nextGlyphIndex([...all, 0])).toBe(1);
  });
});

describe("Sidebar", () => {
  let root: HTMLElement;

  beforeEach(() => {
    root = document.createElement("div");
    document.body.appendChild(root);
    resetInventoryForTesting();
    applyApps([app("chat", { critical: true }), app("terminal")]);
  });

  afterEach(() => {
    m.mount(root, null);
    root.remove();
    resetInventoryForTesting();
  });

  const rows: SidebarTabRow[] = [
    {
      address: "app:terminal?instance=one",
      appName: "terminal",
      appDisplayName: "Terminal",
      label: "terminal one",
      isOpen: true,
      status: "idle",
      renameable: true,
    },
    {
      address: "app:chat?instance=one",
      appName: "chat",
      appDisplayName: "Chat",
      label: "chat one",
      isOpen: false,
      status: "idle",
      renameable: true,
    },
  ];

  function mount(overrides: Partial<SidebarAttrs>): SidebarAttrs {
    const attrs: SidebarAttrs = {
      projects: [project("alpha", { shortcuts: [{ app: "terminal", action: "new", mode: "new" }] }), project("beta")],
      activeViewId: "alpha",
      rows,
      onSelectView: vi.fn(),
      onProjectsChanged: vi.fn(),
      onProjectCreated: vi.fn(),
      onRunShortcut: vi.fn(),
      onRunShortcutAsNew: vi.fn(),
      onFocusLastOfShortcut: vi.fn(),
      onSetShortcutMode: vi.fn(),
      onRemoveShortcut: vi.fn(),
      onPinShortcut: vi.fn(),
      onRunAppAction: vi.fn(),
      awaitingActionKeys: new Set(),
      onOpenRow: vi.fn(),
      onRefreshRow: vi.fn(),
      onRenameRow: vi.fn(),
      onShareApp: vi.fn(),
      onAddRowToProjects: vi.fn(),
      onRemoveFromView: vi.fn(),
      onAppLifecycle: vi.fn(),
      onDeleteRow: vi.fn(),
      ...overrides,
    };
    m.mount(root, { view: () => m(Sidebar, attrs) });
    return attrs;
  }

  function expand(): void {
    root.firstElementChild!.dispatchEvent(new MouseEvent("mouseenter"));
    m.redraw.sync();
  }

  it("runs a project's shortcut and unpins it from the hover control", () => {
    const attrs = mount({});
    root.querySelector<HTMLElement>('[data-shortcut="terminal:new"]')!.click();
    expect(attrs.onRunShortcut).toHaveBeenCalledWith({ app: "terminal", action: "new", mode: "new" });
    root.querySelector<HTMLElement>(".project-rail-shortcut-unpin")!.click();
    expect(attrs.onRemoveShortcut).toHaveBeenCalledWith({ app: "terminal", action: "new", mode: "new" });
  });

  it("shows every app's primary action under Everything with nothing to unpin", () => {
    mount({ activeViewId: EVERYTHING_VIEW_ID });
    const shortcuts = Array.from(root.querySelectorAll<HTMLElement>("[data-shortcut]")).map(
      (el) => el.dataset.shortcut,
    );
    expect(shortcuts).toEqual(["chat:new", "terminal:new"]);
    expect(root.querySelector(".project-rail-shortcut-unpin")).toBeNull();
  });

  it("lists the view's rows once expanded and opens one on click", () => {
    const attrs = mount({});
    expect(root.querySelector("[data-address]")).toBeNull();
    expand();
    const listed = Array.from(root.querySelectorAll<HTMLElement>("[data-address]")).map((el) => el.dataset.address);
    expect(listed).toEqual(["app:terminal?instance=one", "app:chat?instance=one"]);
    root.querySelector<HTMLElement>('[data-address="app:chat?instance=one"]')!.click();
    expect(attrs.onOpenRow).toHaveBeenCalledWith(expect.objectContaining({ address: "app:chat?instance=one" }));
  });

  it("offers the shared verb set on a row's menu, with Remove from project under a project", () => {
    const attrs = mount({});
    expand();
    root.querySelector<HTMLElement>('[aria-label="Actions for terminal one"]')!.click();
    m.redraw.sync();
    const items = Array.from(root.querySelectorAll<HTMLElement>('[role="menuitem"]')).map((el) =>
      el.textContent?.trim(),
    );
    expect(items).toEqual([
      "Refresh",
      "Share Terminal",
      "Add to project...",
      "Rename",
      "Remove from project",
      "Stop Terminal",
      "Delete terminal one",
    ]);
    root.querySelector<HTMLElement>('[role="menuitem"]:last-child')!.click();
    expect(attrs.onDeleteRow).toHaveBeenCalledWith(expect.objectContaining({ address: "app:terminal?instance=one" }));
  });

  it("switches views from the header menu", () => {
    const attrs = mount({});
    root.querySelector<HTMLElement>(".project-rail-header")!.click();
    m.redraw.sync();
    const items = Array.from(root.querySelectorAll<HTMLElement>('[role="menuitem"]'));
    items.find((el) => el.textContent?.includes("Beta"))!.click();
    expect(attrs.onSelectView).toHaveBeenCalledWith("beta");
  });
});
