// @vitest-environment jsdom
/**
 * The installer: it opens on a right-click and yields to one a page already handled, it draws
 * the rows with the default renderer (closed by a press outside, Escape, or a pick), it hands a
 * draft to the connection, it takes a page's own renderer, and it installs once per document.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CONTEXT_MENU_CARD_ATTR,
  CONTEXT_MENU_INSTALLED_ATTR,
  CONTEXT_MENU_ROW_ATTR,
  installElementContextMenu,
  type ContextMenuConnection,
} from "./context_menu";
import { EXPLAIN_PROMPT } from "./context_menu_rows";

const HANDSHAKE = { clientId: "client-1", windowId: "win-1", desktopId: "home", app: "docs", path: "/" };

let connection: ContextMenuConnection & { draftText: ReturnType<typeof vi.fn<(text: string) => void>> };
let uninstall: (() => void) | null = null;

function card(): HTMLElement | null {
  return document.body.querySelector<HTMLElement>(`[${CONTEXT_MENU_CARD_ATTR}]`);
}

function row(key: string): HTMLButtonElement {
  const found = document.body.querySelector<HTMLButtonElement>(`[${CONTEXT_MENU_ROW_ATTR}="${key}"]`);
  if (found === null) throw new Error(`no row ${key}`);
  return found;
}

function rightClick(element: Element, init: MouseEventInit = {}): MouseEvent {
  const event = new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 40, clientY: 50, ...init });
  element.dispatchEvent(event);
  return event;
}

beforeEach(() => {
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: vi.fn(async () => undefined), readText: vi.fn(async () => "") },
    configurable: true,
  });
  document.body.innerHTML = '<p id="para">words</p><input id="field" value="v">';
  connection = { isFramed: true, draftText: vi.fn<(text: string) => void>() };
});

afterEach(() => {
  uninstall?.();
  uninstall = null;
  document.body.innerHTML = "";
});

describe("installElementContextMenu", () => {
  it("opens the menu at the pointer on a right-click, with the reference rows last", () => {
    uninstall = installElementContextMenu({ connection, handshake: () => HANDSHAKE });
    const event = rightClick(document.getElementById("para") as Element);
    expect(event.defaultPrevented).toBe(true);
    const opened = card();
    expect(opened).not.toBeNull();
    expect(opened!.style.left).toBe("40px");
    expect(opened!.style.top).toBe("50px");
    const keys = Array.from(opened!.querySelectorAll(`[${CONTEXT_MENU_ROW_ATTR}]`)).map((button) =>
      button.getAttribute(CONTEXT_MENU_ROW_ATTR),
    );
    expect(keys).toEqual(["copy-element-path", "explain-element", "modify-element"]);
  });

  it("yields to a right-click the page already handled", () => {
    uninstall = installElementContextMenu({ connection, handshake: () => HANDSHAKE });
    const paragraph = document.getElementById("para") as Element;
    paragraph.addEventListener("contextmenu", (event) => event.preventDefault());
    rightClick(paragraph);
    expect(card()).toBeNull();
  });

  it("drafts the prompt and the block through the connection, and closes on the pick", () => {
    uninstall = installElementContextMenu({ connection, handshake: () => HANDSHAKE });
    rightClick(document.getElementById("para") as Element);
    row("explain-element").click();
    expect(card()).toBeNull();
    expect(connection.draftText).toHaveBeenCalledTimes(1);
    const text = connection.draftText.mock.calls[0][0] as string;
    expect(text.startsWith(`${EXPLAIN_PROMPT}\n\n\`\`\`json\n`)).toBe(true);
    expect(text).toContain('"app":"docs"');
    expect(text).toContain('"window_id":"win-1"');
    expect(text).toContain('"id":"para"');
  });

  it("greys the draft rows on a page no shell frames", () => {
    connection = { isFramed: false, draftText: vi.fn<(text: string) => void>() };
    uninstall = installElementContextMenu({ connection, handshake: () => null });
    rightClick(document.getElementById("para") as Element);
    expect(row("explain-element").getAttribute("aria-disabled")).toBe("true");
    row("explain-element").click();
    expect(connection.draftText).not.toHaveBeenCalled();
    expect(card()).not.toBeNull();
  });

  it("closes on Escape and on a press outside, and replaces itself on a second right-click", () => {
    uninstall = installElementContextMenu({ connection, handshake: () => HANDSHAKE });
    const paragraph = document.getElementById("para") as Element;
    rightClick(paragraph);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(card()).toBeNull();
    rightClick(paragraph);
    paragraph.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    expect(card()).toBeNull();
    rightClick(paragraph);
    rightClick(paragraph, { clientX: 70 });
    expect(document.body.querySelectorAll(`[${CONTEXT_MENU_CARD_ATTR}]`)).toHaveLength(1);
    expect(card()!.style.left).toBe("70px");
  });

  it("keeps the open card on a right-click on the card itself, describing none of its own buttons", () => {
    uninstall = installElementContextMenu({ connection, handshake: () => HANDSHAKE });
    rightClick(document.getElementById("para") as Element);
    const opened = card();
    const event = rightClick(row("explain-element"), { clientX: 90 });
    expect(event.defaultPrevented).toBe(true);
    expect(card()).toBe(opened);
    expect(card()!.style.left).toBe("40px");
  });

  it("takes a page's own renderer and its own rows", () => {
    const open = vi.fn();
    uninstall = installElementContextMenu({
      connection,
      handshake: () => HANDSHAKE,
      extraRows: () => [{ kind: "action", key: "rename", label: "Rename", onSelect: () => undefined }],
      open,
    });
    rightClick(document.getElementById("field") as Element);
    expect(card()).toBeNull();
    expect(open).toHaveBeenCalledTimes(1);
    const [rows, point] = open.mock.calls[0] as [{ kind: string; key?: string }[], { x: number; y: number }];
    expect(point).toEqual({ x: 40, y: 50 });
    expect(rows.map((each) => each.key ?? "|")).toEqual([
      "rename",
      "|",
      "paste",
      "select-all",
      "|",
      "copy-element-path",
      "explain-element",
      "modify-element",
    ]);
  });

  it("installs once per document and marks it, and the uninstaller takes the mark off", () => {
    const first = installElementContextMenu({ connection, handshake: () => HANDSHAKE });
    const second = installElementContextMenu({ connection, handshake: () => HANDSHAKE });
    expect(second).toBe(first);
    expect(document.documentElement.hasAttribute(CONTEXT_MENU_INSTALLED_ATTR)).toBe(true);
    rightClick(document.getElementById("para") as Element);
    expect(document.body.querySelectorAll(`[${CONTEXT_MENU_CARD_ATTR}]`)).toHaveLength(1);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    first();
    expect(document.documentElement.hasAttribute(CONTEXT_MENU_INSTALLED_ATTR)).toBe(false);
    const event = rightClick(document.getElementById("para") as Element);
    expect(event.defaultPrevented).toBe(false);
    expect(card()).toBeNull();
  });

  it("yields to a mark another copy of the module left, and its uninstaller leaves that mark alone", () => {
    document.documentElement.setAttribute(CONTEXT_MENU_INSTALLED_ATTR, "");
    const noop = installElementContextMenu({ connection, handshake: () => HANDSHAKE });
    const event = rightClick(document.getElementById("para") as Element);
    expect(event.defaultPrevented).toBe(false);
    expect(card()).toBeNull();
    noop();
    expect(document.documentElement.hasAttribute(CONTEXT_MENU_INSTALLED_ATTR)).toBe(true);
    document.documentElement.removeAttribute(CONTEXT_MENU_INSTALLED_ATTR);
  });
});
