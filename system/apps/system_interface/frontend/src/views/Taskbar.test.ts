// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "../testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { appRecord, desktopRecord, windowRecord } from "../testing/records";
import { Taskbar } from "./Taskbar";
import type { TaskbarAttrs } from "./Taskbar";

afterEach(unmountViews);

function render(overrides: Partial<TaskbarAttrs> = {}): HTMLElement {
  const docs = appRecord("docs");
  const attrs: TaskbarAttrs = {
    entries: [
      {
        window: windowRecord("win-1", "docs", "/a", { title: "Plan" }),
        app: docs,
        title: "Plan",
        isMinimized: false,
        isFocused: true,
      },
      { window: windowRecord("win-2", "docs", "/b"), app: docs, title: "Docs", isMinimized: true, isFocused: false },
      {
        window: windowRecord("win-3", "docs", "/new", { is_settling: true }),
        app: docs,
        title: "Docs",
        isMinimized: true,
        isFocused: false,
      },
    ],
    isCompact: false,
    openEntryMenuWindowId: null,
    launcher: { query: "", isOpen: false, isCompact: false, onOpen: vi.fn(), onClose: vi.fn(), onQuery: vi.fn() },
    tray: {
      desktops: [desktopRecord("home"), desktopRecord("work")],
      activeDesktopId: "home",
      apps: [docs, appRecord("hidden", { internal: true }), appRecord("stopped", { is_running: false })],
      isDesktopsMenuOpen: false,
      openRunningAppName: null,
      onSwitchDesktop: vi.fn(),
      onOpenDesktopsMenu: vi.fn(),
      onDesktopContextMenu: vi.fn(),
      onOpenRunningApp: vi.fn(),
    },
    onEntryClick: vi.fn(),
    onEntryContextMenu: vi.fn(),
    ...overrides,
  };
  const root = mountView(() => m(Taskbar, attrs));
  return root.querySelector("[data-taskbar]") as HTMLElement;
}

describe("Taskbar", () => {
  it("lists one entry per window in opening order with its marks, and reports clicks", () => {
    const onEntryClick = vi.fn();
    const taskbar = render({ onEntryClick });
    const entries = [...taskbar.querySelectorAll("[data-taskbar-entry]")];
    expect(entries.map((entry) => entry.getAttribute("data-taskbar-entry"))).toEqual(["win-1", "win-2", "win-3"]);
    expect(entries.map((entry) => entry.getAttribute("data-minimized"))).toEqual(["false", "true", "true"]);
    expect(entries.map((entry) => entry.getAttribute("data-focused"))).toEqual(["true", "false", "false"]);
    expect(entries.map((entry) => entry.getAttribute("data-settling"))).toEqual(["false", "false", "true"]);
    expect(entries[0].querySelector(".taskbar-entry-title")?.textContent).toBe("Plan");
    (entries[1] as HTMLElement).click();
    expect(onEntryClick).toHaveBeenCalledWith("win-2");
  });

  it("shows icons only in compact mode", () => {
    const taskbar = render({ isCompact: true });
    expect(taskbar.querySelectorAll(".taskbar-entry-title")).toHaveLength(0);
    expect(taskbar.querySelectorAll("[data-taskbar-entry]")).toHaveLength(3);
  });

  it("asks for an entry's menu on a right click", () => {
    const onEntryContextMenu = vi.fn();
    const taskbar = render({ onEntryContextMenu });
    (taskbar.querySelector('[data-taskbar-entry="win-1"]') as HTMLElement).dispatchEvent(
      new MouseEvent("contextmenu", { bubbles: true, clientX: 30, clientY: 40 }),
    );
    expect(onEntryContextMenu).toHaveBeenCalledWith("win-1", 30, 40);
  });

  it("carries the launcher field and the two tray widgets, with a glyph per desktop and an icon per running app", () => {
    const onSwitchDesktop = vi.fn();
    const taskbar = render({
      tray: {
        desktops: [desktopRecord("home"), desktopRecord("work")],
        activeDesktopId: "home",
        apps: [
          appRecord("docs"),
          appRecord("hidden", { internal: true }),
          appRecord("stopped", { is_running: false }),
        ],
        isDesktopsMenuOpen: false,
        openRunningAppName: null,
        onSwitchDesktop,
        onOpenDesktopsMenu: vi.fn(),
        onDesktopContextMenu: vi.fn(),
        onOpenRunningApp: vi.fn(),
      },
    });
    expect(taskbar.querySelector("[data-launcher-field]")).not.toBeNull();
    const switches = [...taskbar.querySelectorAll("[data-desktop-switch]")];
    expect(switches.map((element) => element.getAttribute("data-desktop-switch"))).toEqual(["home", "work"]);
    expect(switches.map((element) => element.getAttribute("data-active"))).toEqual(["true", "false"]);
    (switches[1] as HTMLElement).click();
    expect(onSwitchDesktop).toHaveBeenCalledWith("work");
    expect(
      [...taskbar.querySelectorAll("[data-running-app]")].map((element) => element.getAttribute("data-running-app")),
    ).toEqual(["docs"]);
    expect(taskbar.querySelector('[data-tray-widget="desktops"]')).not.toBeNull();
    expect(taskbar.querySelector('[data-tray-widget="running-apps"]')).not.toBeNull();
  });

  it("the launcher field's Escape clears a typed query first, and keeps the key from the document", () => {
    const onQuery = vi.fn();
    const onClose = vi.fn();
    const onDocumentKeyDown = vi.fn();
    document.addEventListener("keydown", onDocumentKeyDown);
    try {
      const taskbar = render({
        launcher: { query: "docs", isOpen: true, isCompact: false, onOpen: vi.fn(), onClose, onQuery },
      });
      const input = taskbar.querySelector("[data-launcher-field] input") as HTMLInputElement;
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      expect(onQuery).toHaveBeenCalledWith("");
      expect(onClose).not.toHaveBeenCalled();
      expect(onDocumentKeyDown).not.toHaveBeenCalled();
    } finally {
      document.removeEventListener("keydown", onDocumentKeyDown);
    }
  });
});
