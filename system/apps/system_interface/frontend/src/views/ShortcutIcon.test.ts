// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DesktopShortcut } from "../model/records";
import { appRecord, launchPathRecord } from "../testing/records";
import { CONNECTING_TOOLTIP, ShortcutIcon, shortcutLabel, shortcutTooltip } from "./ShortcutIcon";
import type { ShortcutIconAttrs } from "./ShortcutIcon";

const docs = appRecord("docs", { launch_paths: [launchPathRecord({ id: "new", label: "New docs" })] });
const shortcut: DesktopShortcut = {
  target: { kind: "launch", app: "docs", launch: "new" },
  mode: "focus",
  cell: { column: 1, row: 2 },
};

describe("shortcutLabel", () => {
  it("reads the app's name in either mode, and the app name when the app is unknown", () => {
    expect(shortcutLabel(shortcut, docs)).toBe("Docs");
    expect(shortcutLabel({ ...shortcut, mode: "new" }, docs)).toBe("Docs");
    expect(shortcutLabel(shortcut, undefined)).toBe("docs");
  });
});

describe("shortcutTooltip", () => {
  it("says the page is connecting before the apps are known, else why the app is faint, else nothing", () => {
    expect(shortcutTooltip("Docs", false, true)).toBe(CONNECTING_TOOLTIP);
    expect(shortcutTooltip("Docs", true, false)).toBe("Docs: not running");
    expect(shortcutTooltip("Docs", false, false)).toBeNull();
  });
});

afterEach(unmountViews);

function render(overrides: Partial<ShortcutIconAttrs> = {}): HTMLElement {
  const attrs: ShortcutIconAttrs = {
    shortcut,
    cell: { column: 1, row: 2 },
    rect: { x: 112, y: 240, width: 96, height: 112 },
    app: docs,
    isAppsLoaded: true,
    isSelected: false,
    isLifted: false,
    isRunOnClick: false,
    onSelect: vi.fn(),
    onRun: vi.fn(),
    onContextMenu: vi.fn(),
    ...overrides,
  };
  const root = mountView(() => m(ShortcutIcon, attrs));
  return root.querySelector("[data-shortcut]") as HTMLElement;
}

describe("ShortcutIcon", () => {
  it("carries its key and cell, draws in its cell's box, and shows its label", () => {
    const icon = render();
    expect(icon.getAttribute("data-shortcut")).toBe("docs:new");
    expect(icon.getAttribute("data-cell")).toBe("1,2");
    expect(icon.style.left).toBe("112px");
    expect(icon.style.top).toBe("240px");
    expect(icon.querySelector(".shortcut-label")?.textContent).toBe("Docs");
  });

  it("selects on a click, runs on a double click, Enter, or Space, and asks for its menu on a right click", () => {
    const onSelect = vi.fn();
    const onRun = vi.fn();
    const onContextMenu = vi.fn();
    const icon = render({ onSelect, onRun, onContextMenu });
    icon.click();
    expect(onSelect).toHaveBeenCalledTimes(1);
    icon.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    icon.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    icon.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    icon.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));
    expect(onRun).toHaveBeenCalledTimes(3);
    icon.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 5, clientY: 6 }));
    expect(onContextMenu).toHaveBeenCalledWith(5, 6, expect.any(Element));
  });

  it("runs on a click when a click is a run (touch), and the double click a double tap adds runs nothing more", () => {
    const onSelect = vi.fn();
    const onRun = vi.fn();
    const icon = render({ isRunOnClick: true, onSelect, onRun });
    icon.click();
    icon.click();
    icon.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    expect(onRun).toHaveBeenCalledTimes(2);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("marks selection and fades while lifted", () => {
    expect(render({ isSelected: true }).getAttribute("aria-pressed")).toBe("true");
    unmountViews();
    expect(render({ isLifted: true }).className).toContain("opacity-40");
  });

  it("draws an unknown app as connecting only while no app list has landed", () => {
    const connecting = render({ app: undefined, isAppsLoaded: false });
    expect(connecting.getAttribute("data-connecting")).toBe("true");
    expect(connecting.className).toContain("text-faint");
    expect(connecting.querySelector(".shortcut-label")?.textContent).toBe("docs");
    unmountViews();
    const unregistered = render({ app: undefined, isAppsLoaded: true });
    expect(unregistered.hasAttribute("data-connecting")).toBe(false);
    expect(unregistered.className).toContain("text-primary");
    unmountViews();
    expect(render().hasAttribute("data-connecting")).toBe(false);
  });
});
