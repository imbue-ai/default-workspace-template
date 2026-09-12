// @vitest-environment jsdom
/**
 * The menu's behaviour, exercised the way a pointer and a keyboard exercise it. Rendered into
 * a real DOM under jsdom: the menu portals to <body>, so the assertions read <body>.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";

import { createMenu, MENU_PART_ATTR, MENU_ROW_ATTR, type Menu, type MenuRow } from "./menu";

const ANCHOR = { left: 100, right: 140, top: 100, bottom: 120, width: 40 };

function part(name: string): HTMLElement | null {
  return document.body.querySelector<HTMLElement>(`[${MENU_PART_ATTR}="${name}"]`);
}

function row(key: string): HTMLElement {
  const found = document.body.querySelector<HTMLElement>(`[${MENU_ROW_ATTR}="${key}"]`);
  if (found === null) throw new Error(`no row ${key} on screen`);
  return found;
}

let root: HTMLDivElement;
let menu: Menu;
let rows: MenuRow[];

function render(): void {
  m.render(root, menu.view(rows));
}

beforeEach(() => {
  root = document.createElement("div");
  document.body.appendChild(root);
  rows = [];
});

afterEach(() => {
  // What an owner's `onremove` does: a menu left open would keep its Escape listener on the
  // window, and the next test's menu would never hear the key.
  menu.dispose();
  m.render(root, null);
  root.remove();
  document.body.innerHTML = "";
  vi.useRealTimers();
});

describe("a menu", () => {
  beforeEach(() => {
    menu = createMenu({ placement: "below", redraw: render });
  });

  it("is closed until opened, then shows a sheet under the card", () => {
    render();
    expect(part("menu")).toBeNull();
    menu.open(ANCHOR);
    expect(part("menu")).not.toBeNull();
    expect(part("sheet")).not.toBeNull();
    // The sheet renders BEFORE the card, so the card paints over it.
    expect(part("sheet")!.compareDocumentPosition(part("menu")!) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });

  it("closes on a press on the sheet, and on Escape, and on nothing else", () => {
    menu.open(ANCHOR);
    part("sheet")!.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    expect(menu.isOpen()).toBe(false);
    expect(part("menu")).toBeNull();

    menu.open(ANCHOR);
    window.dispatchEvent(new Event("scroll"));
    window.dispatchEvent(new Event("resize"));
    part("menu")!.dispatchEvent(new MouseEvent("mouseleave"));
    expect(menu.isOpen()).toBe(true);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(menu.isOpen()).toBe(false);
  });

  it("runs an action row and closes", () => {
    const picked = vi.fn();
    rows = [{ kind: "action", key: "go", label: "Go", onSelect: picked }];
    menu.open(ANCHOR);
    row("go").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(picked).toHaveBeenCalledTimes(1);
    expect(menu.isOpen()).toBe(false);
  });

  it("ignores a disabled action row and stays open", () => {
    const picked = vi.fn();
    rows = [{ kind: "action", key: "go", label: "Go", isDisabled: true, onSelect: picked }];
    menu.open(ANCHOR);
    expect(row("go").getAttribute("aria-disabled")).toBe("true");
    row("go").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(picked).not.toHaveBeenCalled();
    expect(menu.isOpen()).toBe(true);
  });

  it("leaves the menu up after an action that asks it to", () => {
    const picked = vi.fn();
    rows = [{ kind: "action", key: "stay", label: "Stay", keepsOpen: true, onSelect: picked }];
    menu.open(ANCHOR);
    row("stay").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(picked).toHaveBeenCalledTimes(1);
    expect(menu.isOpen()).toBe(true);
  });

  it("toggles a check row without closing", () => {
    const toggled = vi.fn();
    rows = [{ kind: "check", key: "shown", label: "Shown", isChecked: false, onToggle: toggled }];
    menu.open(ANCHOR);
    row("shown")
      .querySelector("input")!
      .dispatchEvent(new Event("change", { bubbles: true }));
    expect(toggled).toHaveBeenCalledTimes(1);
    expect(menu.isOpen()).toBe(true);
  });

  it("toggles from its trigger attrs, and says so for a screen reader", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const renderTrigger = (): void => m.render(host, m("button", { class: "probe-trigger", ...menu.triggerAttrs() }));
    renderTrigger();
    const trigger = (): HTMLElement => host.querySelector(".probe-trigger") as HTMLElement;
    expect(trigger().getAttribute("aria-expanded")).toBe("false");
    expect(trigger().getAttribute("aria-haspopup")).toBe("menu");
    trigger().dispatchEvent(new MouseEvent("click", { bubbles: true }));
    renderTrigger();
    expect(menu.isOpen()).toBe(true);
    expect(trigger().getAttribute("aria-expanded")).toBe("true");
    trigger().dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(menu.isOpen()).toBe(false);
    m.render(host, null);
  });

  it("tells the caller when it opens and closes", () => {
    const opened = vi.fn();
    const closed = vi.fn();
    menu = createMenu({ placement: "below", redraw: render, onOpen: opened, onClose: closed });
    menu.open(ANCHOR);
    expect(opened).toHaveBeenCalledTimes(1);
    menu.close();
    expect(closed).toHaveBeenCalledTimes(1);
    // Closing a closed menu is not a second close.
    menu.close();
    expect(closed).toHaveBeenCalledTimes(1);
  });
});

describe("a submenu", () => {
  const changes: (string | null)[] = [];

  beforeEach(() => {
    vi.useFakeTimers();
    changes.length = 0;
    menu = createMenu({
      placement: "below",
      redraw: render,
      onSubmenuChange: (key) => changes.push(key),
    });
    rows = [
      {
        kind: "submenu",
        key: "colour",
        label: "Colour",
        value: "Red",
        rows: () => [{ kind: "action", key: "blue", label: "Blue", onSelect: () => undefined }],
      },
      { kind: "action", key: "other", label: "Other", onSelect: () => undefined },
    ];
  });

  it("opens on hover after the intent delay, not before", () => {
    menu.open(ANCHOR);
    row("colour").dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: 110, clientY: 110 }));
    expect(part("submenu")).toBeNull();
    vi.advanceTimersByTime(39);
    expect(part("submenu")).toBeNull();
    vi.advanceTimersByTime(1);
    expect(part("submenu")).not.toBeNull();
    expect(menu.isSubmenuOpen("colour")).toBe(true);
    expect(row("colour").getAttribute("aria-expanded")).toBe("true");
    expect(row("blue")).not.toBeNull();
    expect(changes).toEqual(["colour"]);
  });

  it("does not open for a hover the pointer did not stay for", () => {
    menu.open(ANCHOR);
    row("colour").dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: 110, clientY: 110 }));
    part("menu")!.dispatchEvent(new MouseEvent("mouseleave"));
    vi.advanceTimersByTime(100);
    expect(part("submenu")).toBeNull();
  });

  it("closes once the pointer has left the menu-and-submenu pair, after the grace", () => {
    menu.open(ANCHOR);
    row("colour").dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: 110, clientY: 110 }));
    vi.advanceTimersByTime(40);
    expect(part("submenu")).not.toBeNull();
    part("submenu")!.dispatchEvent(new MouseEvent("mouseleave"));
    vi.advanceTimersByTime(219);
    expect(part("submenu")).not.toBeNull();
    vi.advanceTimersByTime(1);
    expect(part("submenu")).toBeNull();
    // The menu itself is untouched: a drift only ever closes a submenu.
    expect(menu.isOpen()).toBe(true);
    expect(changes).toEqual(["colour", null]);
  });

  it("survives the seam between the two boxes", () => {
    menu.open(ANCHOR);
    row("colour").dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: 110, clientY: 110 }));
    vi.advanceTimersByTime(40);
    part("menu")!.dispatchEvent(new MouseEvent("mouseleave"));
    vi.advanceTimersByTime(100);
    part("submenu")!.dispatchEvent(new MouseEvent("mouseenter"));
    vi.advanceTimersByTime(500);
    expect(part("submenu")).not.toBeNull();
  });

  it("gives way to a row that opens nothing", () => {
    menu.open(ANCHOR);
    row("colour").dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: 110, clientY: 110 }));
    vi.advanceTimersByTime(40);
    row("other").dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: 110, clientY: 150 }));
    vi.advanceTimersByTime(40);
    expect(part("submenu")).toBeNull();
  });

  it("toggles on a click, for a touch screen with no hover to intend anything with", () => {
    menu.open(ANCHOR);
    row("colour").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(part("submenu")).not.toBeNull();
    row("colour").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(part("submenu")).toBeNull();
    expect(menu.isOpen()).toBe(true);
  });

  it("can be closed on its own, leaving the menu up", () => {
    menu.open(ANCHOR);
    row("colour").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(part("submenu")).not.toBeNull();
    menu.closeSubmenu();
    expect(part("submenu")).toBeNull();
    expect(menu.isOpen()).toBe(true);
  });

  it("goes with the menu when the menu closes", () => {
    menu.open(ANCHOR);
    row("colour").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(part("submenu")).not.toBeNull();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(part("submenu")).toBeNull();
    expect(part("menu")).toBeNull();
    expect(changes).toEqual(["colour", null]);
  });

  it("holds custom content when it is given content rather than rows", () => {
    rows = [{ kind: "submenu", key: "custom", label: "Custom", content: () => m("p", { class: "probe" }, "hello") }];
    menu.open(ANCHOR);
    row("custom").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(part("submenu")!.querySelector(".probe")?.textContent).toBe("hello");
  });
});
