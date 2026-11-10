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
  CONTEXT_MENU_SUBMENU_ATTR,
  HOST_MENU_GLOBAL,
  installElementContextMenu,
  type ContextMenuConnection,
  type HostMenuBridge,
} from "./context_menu";
import type { ContextMenuRow } from "./context_menu_rows";
import { REFERENCE_ID_PATTERN } from "./element_reference";

const HANDSHAKE = { clientId: "client-1", windowId: "win-1", desktopId: "home", app: "docs", path: "/" };
const HOST_DRAWN_HANDSHAKE = { ...HANDSHAKE, drawsContextMenus: true };

let connection: ContextMenuConnection & { draftText: ReturnType<typeof vi.fn<(text: string) => void>> };

function fakeConnection(hasShell: boolean): typeof connection {
  return { hasShell: () => hasShell, draftText: vi.fn<(text: string) => void>() };
}
let uninstall: (() => void) | null = null;

function card(): HTMLElement | null {
  return document.body.querySelector<HTMLElement>(`[${CONTEXT_MENU_CARD_ATTR}]`);
}

function row(key: string): HTMLButtonElement {
  const found = document.body.querySelector<HTMLButtonElement>(`[${CONTEXT_MENU_ROW_ATTR}="${key}"]`);
  if (found === null) throw new Error(`no row ${key}`);
  return found;
}

function submenuCard(): HTMLElement | null {
  return document.body.querySelector<HTMLElement>(`[${CONTEXT_MENU_SUBMENU_ATTR}]`);
}

/** A page's own rows with a submenu ("Status": a check row, a divider, an action) and a check row of its own. */
function statusRows(calls: string[], statusChildren: () => ContextMenuRow[] | null = () => null): ContextMenuRow[] {
  return [
    {
      kind: "submenu",
      key: "status",
      label: "Status",
      symbol: "flag",
      rows: () =>
        (statusChildren() as Exclude<ContextMenuRow, { kind: "submenu" }>[] | null) ?? [
          { kind: "check", key: "status-open", label: "Open", isChecked: true, onToggle: () => calls.push("open") },
          { kind: "divider" },
          { kind: "action", key: "status-close", label: "Close", onSelect: () => calls.push("close") },
        ],
    },
    { kind: "check", key: "pinned", label: "Pinned", isChecked: false, onToggle: () => calls.push("pinned") },
    { kind: "action", key: "delete", label: "Delete", tone: "danger", onSelect: () => calls.push("delete") },
  ];
}

function hostBridge(): HostMenuBridge {
  const bridge = Reflect.get(window, HOST_MENU_GLOBAL) as HostMenuBridge | undefined;
  if (bridge === undefined) throw new Error("no host menu bridge");
  return bridge;
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
  connection = fakeConnection(true);
});

afterEach(() => {
  uninstall?.();
  uninstall = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("installElementContextMenu", () => {
  it("opens a link from the menu by clicking it, so the page's link rule follows it as it would a click", () => {
    document.body.innerHTML = '<div class="message"><a id="file" href="file:///home/user/plan.md">plan</a></div>';
    const linkClicks: Event[] = [];
    // Stands in for the app contract's listener, which takes a link's click instead of the page.
    const takeClick = (event: Event): void => {
      if (!(event.target instanceof HTMLAnchorElement)) return;
      event.preventDefault();
      linkClicks.push(event);
    };
    window.addEventListener("click", takeClick);
    uninstall = installElementContextMenu({ connection, handshake: () => HANDSHAKE });
    rightClick(document.getElementById("file") as Element);
    row("open-link").click();
    window.removeEventListener("click", takeClick);
    expect(linkClicks.map((event) => event.target)).toEqual([document.getElementById("file")]);
  });

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
    expect(keys).toEqual(["copy-reference", "explain-element", "modify-element"]);
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
    const [prompt, blank, fence] = text.split("\n");
    expect(prompt.startsWith("Explain what I attached in REF-")).toBe(true);
    expect(prompt.slice("Explain what I attached in ".length)).toMatch(REFERENCE_ID_PATTERN);
    expect([blank, fence]).toEqual(["", "```json"]);
    expect(text).toContain('"app":"docs"');
    expect(text).toContain('"window_id":"win-1"');
    expect(text).toContain('"id":"para"');
  });

  it("takes a draft route and a link route of the page's own in place of a connection", () => {
    document.body.innerHTML += '<a id="docs" href="https://example.com/docs">docs</a>';
    const draft = vi.fn<(text: string) => void>();
    const openLink = vi.fn<(anchor: HTMLAnchorElement) => void>();
    uninstall = installElementContextMenu({ draft, isDraftAvailable: () => true, openLink, handshake: () => null });
    rightClick(document.getElementById("para") as Element);
    expect(row("explain-element").getAttribute("aria-disabled")).toBeNull();
    row("explain-element").click();
    expect(draft).toHaveBeenCalledTimes(1);
    expect(draft.mock.calls[0][0]).toContain('"app":null');
    rightClick(document.getElementById("docs") as Element);
    row("open-link").click();
    expect(openLink.mock.calls).toEqual([[document.getElementById("docs")]]);
  });

  it("greys the draft rows on a page no shell frames", () => {
    connection = fakeConnection(false);
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

  it("keeps a press on a row from taking focus off the target", () => {
    uninstall = installElementContextMenu({ connection, handshake: () => HANDSHAKE });
    rightClick(document.getElementById("para") as Element);
    const press = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    row("explain-element").dispatchEvent(press);
    expect(press.defaultPrevented).toBe(true);
    expect(card()).not.toBeNull();
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
    const [rows, point, target] = open.mock.calls[0] as [
      { kind: string; key?: string }[],
      { x: number; y: number },
      { element: Element },
    ];
    expect(point).toEqual({ x: 40, y: 50 });
    expect(target.element).toBe(document.getElementById("field"));
    expect(rows.map((each) => each.key ?? "|")).toEqual([
      "rename",
      "|",
      "paste",
      "select-all",
      "|",
      "copy-reference",
      "explain-element",
      "modify-element",
    ]);
  });

  it("leaves a right-click with Shift held to the browser's own menu", () => {
    uninstall = installElementContextMenu({ connection, handshake: () => HANDSHAKE });
    const event = rightClick(document.getElementById("para") as Element, { shiftKey: true });
    expect(event.defaultPrevented).toBe(false);
    expect(card()).toBeNull();
  });

  it("where the desktop app draws the menu, opens nothing and keeps the rows for it, with the link row the page follows", () => {
    document.body.innerHTML += '<a id="docs" href="https://example.com/docs">docs</a>';
    uninstall = installElementContextMenu({
      connection,
      handshake: () => HOST_DRAWN_HANDSHAKE,
      extraRows: () => [
        { kind: "action", key: "rename", label: "Rename", symbol: "pencil.line", onSelect: () => undefined },
      ],
    });
    const event = rightClick(document.getElementById("docs") as Element);
    expect(event.defaultPrevented).toBe(false);
    expect(card()).toBeNull();
    const sections = hostBridge().sections();
    expect(sections).not.toBeNull();
    const keysOf = (items: { kind: string; key?: string }[]): string[] => items.map((item) => item.key ?? "|");
    expect(keysOf(sections!.page)).toEqual(["rename"]);
    expect(keysOf(sections!.link)).toEqual(["open-link"]);
    expect(sections!.reference).toEqual([
      expect.objectContaining({ key: "copy-reference", isDisabled: false, symbol: "doc.on.doc" }),
      expect.objectContaining({ key: "explain-element", isDisabled: false, symbol: "questionmark.bubble" }),
      expect.objectContaining({ key: "modify-element", isDisabled: false, symbol: "pencil" }),
    ]);
    expect(sections!.page[0]).toEqual({
      kind: "action",
      key: "rename",
      label: "Rename",
      isDisabled: false,
      tooltip: null,
      symbol: "pencil.line",
    });
  });

  it("hands the desktop app a submenu with its rows read at the right-click, and runs a row picked inside it", async () => {
    const calls: string[] = [];
    let children: ContextMenuRow[] | null = null;
    uninstall = installElementContextMenu({
      connection,
      handshake: () => HOST_DRAWN_HANDSHAKE,
      extraRows: () => statusRows(calls, () => children),
    });
    rightClick(document.getElementById("para") as Element);
    // A submenu that changes after the right-click changes neither what the app shows nor what a pick runs.
    children = [{ kind: "action", key: "status-later", label: "Later", onSelect: () => calls.push("later") }];
    const page = hostBridge().sections()!.page;
    expect(page).toEqual([
      {
        kind: "submenu",
        label: "Status",
        tooltip: null,
        symbol: "flag",
        items: [
          { kind: "check", key: "status-open", label: "Open", isChecked: true, tooltip: null, symbol: null },
          { kind: "divider" },
          { kind: "action", key: "status-close", label: "Close", isDisabled: false, tooltip: null, symbol: null },
        ],
      },
      { kind: "check", key: "pinned", label: "Pinned", isChecked: false, tooltip: null, symbol: null },
      { kind: "action", key: "delete", label: "Delete", isDisabled: false, tooltip: null, symbol: null },
    ]);
    await hostBridge().select("status-close");
    await hostBridge().select("status-open");
    await hostBridge().select("pinned");
    await hostBridge().select("status-later");
    expect(calls).toEqual(["close", "open", "pinned"]);
  });

  it("runs the row the desktop app picks only once the page has focus, so the clipboard takes the copy", async () => {
    uninstall = installElementContextMenu({ connection, handshake: () => HOST_DRAWN_HANDSHAKE });
    rightClick(document.getElementById("para") as Element);
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    const picked = hostBridge().select("copy-reference");
    await Promise.resolve();
    expect(navigator.clipboard.writeText).not.toHaveBeenCalled();
    window.dispatchEvent(new Event("focus"));
    await picked;
    expect(navigator.clipboard.writeText).toHaveBeenCalledTimes(1);
    expect(vi.mocked(navigator.clipboard.writeText).mock.calls[0][0]).toContain('"id":"para"');
  });

  it("runs a picked row after the wait for focus even if focus never comes", async () => {
    vi.useFakeTimers();
    uninstall = installElementContextMenu({ connection, handshake: () => HOST_DRAWN_HANDSHAKE });
    rightClick(document.getElementById("para") as Element);
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    const picked = hostBridge().select("explain-element");
    await vi.runAllTimersAsync();
    await picked;
    expect(connection.draftText).toHaveBeenCalledTimes(1);
  });

  it("runs no disabled row and no row of an earlier right-click the browser's menu took", async () => {
    connection = fakeConnection(false);
    uninstall = installElementContextMenu({ connection, handshake: () => HOST_DRAWN_HANDSHAKE });
    const paragraph = document.getElementById("para") as Element;
    rightClick(paragraph);
    await hostBridge().select("explain-element");
    expect(connection.draftText).not.toHaveBeenCalled();
    rightClick(paragraph, { shiftKey: true });
    expect(hostBridge().sections()).toBeNull();
    await hostBridge().select("copy-reference");
    expect(navigator.clipboard.writeText).not.toHaveBeenCalled();
  });

  it("keeps no rows for a right-click a page handler stopped short of the document", () => {
    document.body.innerHTML += '<div id="stops">stops</div>';
    document.getElementById("stops")!.addEventListener("contextmenu", (event) => event.stopPropagation());
    uninstall = installElementContextMenu({ connection, handshake: () => HOST_DRAWN_HANDSHAKE });
    rightClick(document.getElementById("para") as Element);
    expect(hostBridge().sections()).not.toBeNull();
    rightClick(document.getElementById("stops") as Element);
    expect(hostBridge().sections()).toBeNull();
  });

  it("takes the bridge off the page when uninstalled", () => {
    const remove = installElementContextMenu({ connection, handshake: () => HOST_DRAWN_HANDSHAKE });
    expect(Reflect.has(window, HOST_MENU_GLOBAL)).toBe(true);
    remove();
    expect(Reflect.has(window, HOST_MENU_GLOBAL)).toBe(false);
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

  it("takes an open card down with its listeners when uninstalled", () => {
    uninstall = installElementContextMenu({ connection, handshake: () => HANDSHAKE });
    rightClick(document.getElementById("para") as Element);
    expect(card()).not.toBeNull();
    uninstall();
    uninstall = null;
    expect(card()).toBeNull();
    const escape = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    document.dispatchEvent(escape);
    expect(escape.defaultPrevented).toBe(false);
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

describe("the framework-free renderer", () => {
  function installWithStatusRows(calls: string[]): void {
    uninstall = installElementContextMenu({
      connection,
      handshake: () => HANDSHAKE,
      extraRows: () => statusRows(calls),
    });
    rightClick(document.getElementById("para") as Element);
  }

  it("opens a submenu once the pointer rests on its row, and a pick inside it runs and closes both cards", () => {
    vi.useFakeTimers();
    const calls: string[] = [];
    installWithStatusRows(calls);
    expect(row("status").getAttribute("aria-haspopup")).toBe("menu");
    row("status").dispatchEvent(new MouseEvent("mouseenter"));
    expect(submenuCard()).toBeNull();
    vi.advanceTimersByTime(150);
    expect(submenuCard()).not.toBeNull();
    expect(row("status-open").getAttribute("aria-checked")).toBe("true");
    row("status-close").click();
    expect(calls).toEqual(["close"]);
    expect(card()).toBeNull();
    expect(submenuCard()).toBeNull();
  });

  it("opens a submenu at once on a click, and a press inside it keeps the menu up", () => {
    const calls: string[] = [];
    installWithStatusRows(calls);
    row("status").click();
    expect(submenuCard()).not.toBeNull();
    submenuCard()!.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    expect(card()).not.toBeNull();
    row("status-open").click();
    expect(calls).toEqual(["open"]);
    expect(card()).toBeNull();
  });

  it("closes a submenu once the pointer has sat on another row past the grace, unless it reaches the submenu", () => {
    vi.useFakeTimers();
    installWithStatusRows([]);
    row("status").click();
    row("pinned").dispatchEvent(new MouseEvent("mouseenter"));
    vi.advanceTimersByTime(100);
    submenuCard()!.dispatchEvent(new MouseEvent("mouseenter"));
    vi.advanceTimersByTime(1000);
    expect(submenuCard()).not.toBeNull();
    row("pinned").dispatchEvent(new MouseEvent("mouseenter"));
    vi.advanceTimersByTime(1000);
    expect(submenuCard()).toBeNull();
    expect(card()).not.toBeNull();
  });

  it("opens a submenu after the menu's right edge, or before its left edge where the right would leave the viewport", () => {
    const boxes = new Map<string, DOMRect>([
      ["menu-right", new DOMRect(100, 100, 200, 120)],
      ["menu-left", new DOMRect(820, 100, 200, 120)],
    ]);
    let menuBox = boxes.get("menu-right")!;
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(180);
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(90);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.hasAttribute(CONTEXT_MENU_CARD_ATTR)) return menuBox;
      return new DOMRect(menuBox.left, 140, 200, 32);
    });
    Object.defineProperty(window, "innerWidth", { value: 1024, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 768, configurable: true });
    installWithStatusRows([]);
    row("status").click();
    expect(submenuCard()!.style.left).toBe("296px");
    expect(submenuCard()!.style.top).toBe("136px");
    menuBox = boxes.get("menu-left")!;
    rightClick(document.getElementById("para") as Element);
    row("status").click();
    expect(submenuCard()!.style.left).toBe("644px");
  });

  it("toggles a check row and closes, and draws a danger row in red", () => {
    const calls: string[] = [];
    installWithStatusRows(calls);
    expect(row("pinned").getAttribute("role")).toBe("menuitemcheckbox");
    expect(row("pinned").getAttribute("aria-checked")).toBe("false");
    expect(row("delete").style.color).toBe("rgb(198, 40, 40)");
    row("pinned").click();
    expect(calls).toEqual(["pinned"]);
    expect(card()).toBeNull();
  });

  it("takes an open submenu down with the menu on Escape", () => {
    installWithStatusRows([]);
    row("status").click();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(card()).toBeNull();
    expect(submenuCard()).toBeNull();
  });
});
