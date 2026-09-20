// @vitest-environment jsdom
import "../testing/dom";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Menu, placeMenu } from "./Menu";
import type { MenuEntry } from "./Menu";

const VIEWPORT = { width: 1000, height: 800 };
const SIZE = { width: 200, height: 100 };

describe("placeMenu", () => {
  it("hangs under its anchor, and flips above when it would overflow the bottom", () => {
    const anchor = { left: 100, right: 150, top: 700, bottom: 740, width: 50 };
    expect(placeMenu(anchor, SIZE, VIEWPORT, "below")).toEqual({ left: 100, top: 600 });
    const high = { left: 100, right: 150, top: 100, bottom: 140, width: 50 };
    expect(placeMenu(high, SIZE, VIEWPORT, "below")).toEqual({ left: 100, top: 140 });
  });

  it("sits beside its anchor, flipping to the left when it would overflow the right", () => {
    const anchor = { left: 900, right: 950, top: 100, bottom: 140, width: 50 };
    expect(placeMenu(anchor, SIZE, VIEWPORT, "right")).toEqual({ left: 700, top: 100 });
  });

  it("clamps inside the viewport margin", () => {
    const anchor = { left: 990, right: 995, top: 795, bottom: 799, width: 5 };
    expect(placeMenu(anchor, SIZE, VIEWPORT, "below")).toEqual({ left: 794, top: 694 });
  });
});

let root: HTMLElement | null = null;

afterEach(() => {
  if (root !== null) {
    m.mount(root, null);
    root.remove();
    root = null;
  }
  document.body.innerHTML = "";
});

function mountMenu(entries: MenuEntry[], onClose: () => void, isInsideTrigger?: (target: Node) => boolean): void {
  root = document.createElement("div");
  document.body.appendChild(root);
  const anchor = { left: 10, right: 20, top: 10, bottom: 20, width: 10 };
  m.mount(root, {
    view: () => m(Menu, { anchor, placement: "below", marker: "test-menu", entries, onClose, isInsideTrigger }),
  });
}

describe("Menu", () => {
  it("renders its rows into the body, runs a row's action after closing, and skips a disabled row", () => {
    const ran: string[] = [];
    const onClose = vi.fn();
    mountMenu(
      [
        { key: "one", label: "One", run: () => void ran.push("one") },
        "divider",
        { key: "two", label: "Two", isDisabled: true, run: () => void ran.push("two") },
      ],
      onClose,
    );
    const card = document.querySelector('[data-floating="test-menu"]');
    expect(card).not.toBeNull();
    expect(card?.getAttribute("role")).toBe("menu");
    (document.querySelector('[data-menu-item="one"]') as HTMLElement).click();
    (document.querySelector('[data-menu-item="two"]') as HTMLElement).click();
    expect(ran).toEqual(["one"]);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes on a press outside the card and on Escape, not on a press inside", () => {
    const onClose = vi.fn();
    mountMenu([{ key: "one", label: "One", run: () => undefined }], onClose);
    const row = document.querySelector('[data-menu-item="one"]') as HTMLElement;
    row.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    expect(onClose).not.toHaveBeenCalled();
    document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    expect(onClose).toHaveBeenCalledTimes(1);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("stays open through a press on its trigger, so the trigger's click can toggle it closed", () => {
    const onClose = vi.fn();
    const trigger = document.createElement("button");
    trigger.setAttribute("data-trigger", "");
    document.body.appendChild(trigger);
    mountMenu(
      [{ key: "one", label: "One", run: () => undefined }],
      onClose,
      (target) => target instanceof Element && target.closest("[data-trigger]") !== null,
    );
    trigger.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    expect(onClose).not.toHaveBeenCalled();
    document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
