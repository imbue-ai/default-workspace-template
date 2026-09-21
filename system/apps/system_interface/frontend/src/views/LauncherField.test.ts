// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LauncherField, isSecondaryChord } from "./LauncherField";
import type { LauncherFieldAttrs } from "./LauncherField";

afterEach(unmountViews);

function render(overrides: Partial<LauncherFieldAttrs> = {}): { root: HTMLElement; attrs: LauncherFieldAttrs } {
  const attrs: LauncherFieldAttrs = {
    query: "",
    isOpen: true,
    isCompact: false,
    onOpen: vi.fn(),
    onClose: vi.fn(),
    onQuery: vi.fn(),
    onMoveHighlight: vi.fn(),
    onRunHighlight: vi.fn(),
    onRunSecondary: vi.fn(),
    ...overrides,
  };
  return { root: mountView(() => m(LauncherField, attrs)), attrs };
}

function inputOf(root: HTMLElement): HTMLInputElement {
  return root.querySelector("[data-launcher-field] input") as HTMLInputElement;
}

function press(input: HTMLInputElement, init: KeyboardEventInit): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  input.dispatchEvent(event);
  return event;
}

describe("isSecondaryChord", () => {
  it("is Enter with Ctrl or with Cmd, and nothing else", () => {
    expect(isSecondaryChord({ key: "Enter", ctrlKey: true, metaKey: false })).toBe(true);
    expect(isSecondaryChord({ key: "Enter", ctrlKey: false, metaKey: true })).toBe(true);
    expect(isSecondaryChord({ key: "Enter", ctrlKey: false, metaKey: false })).toBe(false);
    expect(isSecondaryChord({ key: "a", ctrlKey: true, metaKey: false })).toBe(false);
  });
});

describe("the launcher field", () => {
  it("the arrows move the highlight and open the menu; Enter runs the highlight", () => {
    const { root, attrs } = render({ isOpen: false });
    const input = inputOf(root);
    expect(press(input, { key: "ArrowDown" }).defaultPrevented).toBe(true);
    expect(attrs.onMoveHighlight).toHaveBeenCalledWith(1);
    expect(press(input, { key: "ArrowUp" }).defaultPrevented).toBe(true);
    expect(attrs.onMoveHighlight).toHaveBeenCalledWith(-1);
    expect(attrs.onOpen).toHaveBeenCalledTimes(2);
    expect(press(input, { key: "Enter" }).defaultPrevented).toBe(true);
    expect(attrs.onRunHighlight).toHaveBeenCalledTimes(1);
    expect(attrs.onRunSecondary).not.toHaveBeenCalled();
  });

  it("Ctrl+Enter and Cmd+Enter run the secondary action, never the highlight", () => {
    const { root, attrs } = render();
    const input = inputOf(root);
    expect(press(input, { key: "Enter", ctrlKey: true }).defaultPrevented).toBe(true);
    press(input, { key: "Enter", metaKey: true });
    expect(attrs.onRunSecondary).toHaveBeenCalledTimes(2);
    expect(attrs.onRunHighlight).not.toHaveBeenCalled();
  });

  it("typing reports the query and opens the menu", () => {
    const { root, attrs } = render({ isOpen: false });
    const input = inputOf(root);
    input.value = "term";
    input.dispatchEvent(new InputEvent("input", { bubbles: true }));
    expect(attrs.onQuery).toHaveBeenCalledWith("term");
    expect(attrs.onOpen).toHaveBeenCalledTimes(1);
  });

  it("Escape on an empty field closes the menu and blurs, and keeps the key from the document", () => {
    const onDocumentKeyDown = vi.fn();
    document.addEventListener("keydown", onDocumentKeyDown);
    try {
      const { root, attrs } = render();
      const input = inputOf(root);
      input.focus();
      expect(document.activeElement).toBe(input);
      press(input, { key: "Escape" });
      expect(attrs.onClose).toHaveBeenCalledTimes(1);
      expect(attrs.onQuery).not.toHaveBeenCalled();
      expect(document.activeElement).not.toBe(input);
      expect(onDocumentKeyDown).not.toHaveBeenCalled();
    } finally {
      document.removeEventListener("keydown", onDocumentKeyDown);
    }
  });

  it("in compact mode with the menu closed it is a button that opens the menu", () => {
    const { root, attrs } = render({ isCompact: true, isOpen: false });
    expect(root.querySelector("[data-launcher-field] input")).toBeNull();
    const toggle = root.querySelector("[data-launcher-field]") as HTMLElement;
    expect(toggle.tagName).toBe("BUTTON");
    toggle.click();
    expect(attrs.onOpen).toHaveBeenCalledTimes(1);
  });
});
