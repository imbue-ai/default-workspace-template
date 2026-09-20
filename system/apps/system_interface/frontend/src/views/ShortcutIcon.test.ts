// @vitest-environment jsdom
import "../testing/dom";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DesktopShortcut } from "../model/records";
import { appRecord, launchPathRecord } from "../testing/records";
import { ShortcutIcon, shortcutLabel } from "./ShortcutIcon";
import type { ShortcutIconAttrs } from "./ShortcutIcon";

const docs = appRecord("docs", { launch_paths: [launchPathRecord({ id: "new", label: "New docs" })] });
const shortcut: DesktopShortcut = {
  target: { kind: "launch", app: "docs", launch: "new" },
  mode: "focus",
  cell: { column: 1, row: 2 },
};

describe("shortcutLabel", () => {
  it("reads the app's name while focusing and the launch path's label while always creating", () => {
    expect(shortcutLabel(shortcut, docs)).toBe("Docs");
    expect(shortcutLabel({ ...shortcut, mode: "new" }, docs)).toBe("New docs");
    expect(shortcutLabel(shortcut, undefined)).toBe("docs");
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

function render(overrides: Partial<ShortcutIconAttrs> = {}): HTMLElement {
  root = document.createElement("div");
  document.body.appendChild(root);
  const attrs: ShortcutIconAttrs = {
    shortcut,
    cell: { column: 1, row: 2 },
    rect: { x: 112, y: 240, width: 96, height: 112 },
    app: docs,
    isSelected: false,
    isLifted: false,
    onSelect: vi.fn(),
    onRun: vi.fn(),
    onContextMenu: vi.fn(),
    ...overrides,
  };
  m.mount(root, { view: () => m(ShortcutIcon, attrs) });
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
    expect(onContextMenu).toHaveBeenCalledWith(5, 6);
  });

  it("marks selection and fades while lifted", () => {
    expect(render({ isSelected: true }).getAttribute("aria-pressed")).toBe("true");
    m.mount(root as HTMLElement, null);
    expect(render({ isLifted: true }).className).toContain("opacity-40");
  });
});
