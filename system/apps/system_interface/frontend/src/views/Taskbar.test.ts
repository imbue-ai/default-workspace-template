// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { applyPresence, resetPresenceForTesting } from "../model/Presence";
import { appRecord, avatarStateRecord, desktopRecord, presentUserRecord, windowRecord } from "../testing/records";
import { Taskbar } from "./Taskbar";
import type { TaskbarAttrs } from "./Taskbar";

afterEach(() => {
  unmountViews();
  resetPresenceForTesting();
});

function render(overrides: Partial<TaskbarAttrs> = {}): HTMLElement {
  const docs = appRecord("docs");
  const attrs: TaskbarAttrs = {
    avatar: avatarStateRecord(),
    entries: [
      {
        window: windowRecord("win-1", "docs", "/a", { title: "Plan" }),
        app: docs,
        title: "Plan",
        isMinimized: false,
        isDetached: false,
        isFocused: true,
        isPinned: false,
        look: null,
      },
      {
        window: windowRecord("win-2", "docs", "/new"),
        app: docs,
        title: "Docs",
        isMinimized: true,
        isDetached: false,
        isFocused: false,
        isPinned: false,
        look: null,
      },
      {
        window: windowRecord("win-3", "docs", "/", { is_pinned: true }),
        app: docs,
        title: "Docs",
        isMinimized: true,
        isDetached: false,
        isFocused: false,
        isPinned: true,
        look: { mode: "bar", style: "avatar", declaredStyle: "avatar", position: null },
      },
    ],
    isCompact: false,
    openEntryMenuWindowId: null,
    launcher: {
      query: "",
      isOpen: false,
      isCompact: false,
      onOpen: vi.fn(),
      onClose: vi.fn(),
      onQuery: vi.fn(),
      onMoveHighlight: vi.fn(),
      onRunHighlight: vi.fn(),
      onRunSecondary: vi.fn(),
      onRise: vi.fn(),
    },
    tray: {
      desktops: [desktopRecord("home"), desktopRecord("work")],
      activeDesktopId: "home",
      isDesktopsMenuOpen: false,
      onSwitchDesktop: vi.fn(),
      onOpenDesktopsMenu: vi.fn(),
      onDesktopContextMenu: vi.fn(),
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
    expect(entries.map((entry) => entry.getAttribute("data-pinned"))).toEqual(["false", "false", "true"]);
    expect(entries.map((entry) => entry.getAttribute("data-pinned-entry"))).toEqual([null, null, "docs"]);
    expect(entries[2].getAttribute("data-entry-mode")).toBe("bar");
    expect(entries[2].getAttribute("data-entry-style")).toBe("avatar");
    expect(entries[0].querySelector(".taskbar-entry-title")?.textContent).toBe("Plan");
    (entries[1] as HTMLElement).click();
    expect(onEntryClick).toHaveBeenCalledWith("win-2");
  });

  it("marks an entry whose window is shown in its own desktop window, dimmed like a minimized one", () => {
    const docs = appRecord("docs");
    const taskbar = render({
      entries: [
        {
          window: windowRecord("win-1", "docs", "/a", { title: "Plan" }),
          app: docs,
          title: "Plan",
          isMinimized: false,
          isDetached: true,
          isFocused: false,
          isPinned: false,
          look: null,
        },
        {
          window: windowRecord("win-2", "docs", "/new"),
          app: docs,
          title: "Docs",
          isMinimized: false,
          isDetached: false,
          isFocused: true,
          isPinned: false,
          look: null,
        },
      ],
    });
    const entries = [...taskbar.querySelectorAll("[data-taskbar-entry]")];
    expect(entries.map((entry) => entry.getAttribute("data-detached"))).toEqual(["true", "false"]);
    expect(entries.map((entry) => entry.getAttribute("data-minimized"))).toEqual(["false", "false"]);
    expect(entries.map((entry) => entry.querySelector('[aria-label="In its own window"]') !== null)).toEqual([
      true,
      false,
    ]);
    expect(entries.map((entry) => entry.querySelector(".opacity-60") !== null)).toEqual([true, false]);
  });

  it("shows icons only in compact mode, the title as each entry's accessible name", () => {
    const taskbar = render({ isCompact: true });
    expect(taskbar.querySelectorAll(".taskbar-entry-title")).toHaveLength(0);
    const entries = taskbar.querySelectorAll("[data-taskbar-entry]");
    expect(entries).toHaveLength(3);
    expect([...entries].map((entry) => entry.getAttribute("aria-label"))).toEqual(["Plan", "Docs", "Docs"]);
    expect(render().querySelector("[data-taskbar-entry]")?.getAttribute("aria-label")).toBeNull();
  });

  it("draws the avatar in place of the icon for an avatar-style entry, wearing the mood, image only when compact", () => {
    const stale = avatarStateRecord({ design: "jelly-cat", status: { mood: "working", is_stale: true } });
    const taskbar = render({ avatar: stale });
    const entry = taskbar.querySelector('[data-taskbar-entry="win-3"]') as HTMLElement;
    expect(entry.getAttribute("data-mood")).toBe("working");
    expect(entry.getAttribute("data-stale")).toBe("true");
    expect(entry.getAttribute("data-hover-tooltip")).toBe("Docs (status may be out of date)");
    expect(entry.querySelector("svg")).toBeNull();
    expect(entry.querySelector("img")?.getAttribute("src")).toBe("/api/avatars/jelly-cat/image.svg?mood=working");
    expect(entry.querySelector(".taskbar-entry-title")?.textContent).toBe("Docs");
    const ordinary = taskbar.querySelector('[data-taskbar-entry="win-1"]') as HTMLElement;
    expect(ordinary.getAttribute("data-mood")).toBeNull();
    expect(ordinary.querySelector("svg")).not.toBeNull();
    const compact = render({ avatar: stale, isCompact: true }).querySelector(
      '[data-taskbar-entry="win-3"]',
    ) as HTMLElement;
    expect(compact.querySelector("img")).not.toBeNull();
    expect(compact.querySelector(".taskbar-entry-title")).toBeNull();
  });

  it("asks for an entry's menu on a right click", () => {
    const onEntryContextMenu = vi.fn();
    const taskbar = render({ onEntryContextMenu });
    (taskbar.querySelector('[data-taskbar-entry="win-1"]') as HTMLElement).dispatchEvent(
      new MouseEvent("contextmenu", { bubbles: true, clientX: 30, clientY: 40 }),
    );
    expect(onEntryContextMenu).toHaveBeenCalledWith("win-1", 30, 40, expect.any(Element));
  });

  it("carries the launcher field and the Desktops widget with a glyph per desktop, and nothing else in the tray while nobody is recorded", () => {
    const onSwitchDesktop = vi.fn();
    const taskbar = render({
      tray: {
        desktops: [desktopRecord("home"), desktopRecord("work")],
        activeDesktopId: "home",
        isDesktopsMenuOpen: false,
        onSwitchDesktop,
        onOpenDesktopsMenu: vi.fn(),
        onDesktopContextMenu: vi.fn(),
      },
    });
    expect(taskbar.querySelector("[data-launcher-field]")).not.toBeNull();
    const switches = [...taskbar.querySelectorAll("[data-desktop-switch]")];
    expect(switches.map((element) => element.getAttribute("data-desktop-switch"))).toEqual(["home", "work"]);
    expect(switches.map((element) => element.getAttribute("data-active"))).toEqual(["true", "false"]);
    (switches[1] as HTMLElement).click();
    expect(onSwitchDesktop).toHaveBeenCalledWith("work");
    expect(taskbar.querySelector('[data-tray-widget="desktops"]')).not.toBeNull();
    expect(taskbar.querySelector("[data-system-tray]")?.children).toHaveLength(1);
  });

  it("draws the Presence widget in front of Desktops once two users are connected", () => {
    applyPresence([presentUserRecord("user-bob-4471"), presentUserRecord("user-owner-9c21", { owner: true })]);
    const taskbar = render();
    const tray = taskbar.querySelector("[data-system-tray]") as HTMLElement;
    expect([...tray.children].map((widget) => widget.getAttribute("data-tray-widget"))).toEqual([
      "presence",
      "desktops",
    ]);
    expect(
      [...tray.querySelectorAll("[data-presence-user]")].map((el) => el.getAttribute("data-presence-user")),
    ).toEqual(["user-bob-4471", "user-owner-9c21"]);
  });

  it("the launcher field's Escape clears a typed query first, and keeps the key from the document", () => {
    const onQuery = vi.fn();
    const onClose = vi.fn();
    const onDocumentKeyDown = vi.fn();
    document.addEventListener("keydown", onDocumentKeyDown);
    try {
      const taskbar = render({
        launcher: {
          query: "docs",
          isOpen: true,
          isCompact: false,
          onOpen: vi.fn(),
          onClose,
          onQuery,
          onMoveHighlight: vi.fn(),
          onRunHighlight: vi.fn(),
          onRunSecondary: vi.fn(),
          onRise: vi.fn(),
        },
      });
      const input = taskbar.querySelector("[data-launcher-field] textarea") as HTMLTextAreaElement;
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      expect(onQuery).toHaveBeenCalledWith("");
      expect(onClose).not.toHaveBeenCalled();
      expect(onDocumentKeyDown).not.toHaveBeenCalled();
    } finally {
      document.removeEventListener("keydown", onDocumentKeyDown);
    }
  });
});
