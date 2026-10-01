// @vitest-environment jsdom
/**
 * A phone sheet's dismissal: dragging its head down past a quarter of the panel dismisses it, a shorter drag springs
 * it back, and a press on a control in the head is the control's; Escape dismisses it unless a menu over it is open.
 */
import "../../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Sheet } from "./Sheet";

const PANEL_HEIGHT = 400;
let onDismiss = vi.fn<() => void>();

// jsdom has no pointer capture.
Element.prototype.setPointerCapture ??= () => undefined;

function mountSheet(): { grip: HTMLElement; panel: HTMLElement; button: HTMLElement } {
  const root = mountView(() =>
    m(Sheet, { name: "windows", onDismiss, head: m("button", { type: "button", "data-head-button": "" }) }, "rows"),
  );
  const panel = root.querySelector<HTMLElement>('[data-phone-sheet="windows"]');
  const grip = root.querySelector<HTMLElement>(".phone-sheet-head");
  const button = root.querySelector<HTMLElement>("[data-head-button]");
  if (panel === null || grip === null || button === null) throw new Error("no sheet");
  // jsdom lays nothing out: the panel is given the height a phone gives it.
  panel.getBoundingClientRect = () => ({ height: PANEL_HEIGHT }) as DOMRect;
  return { grip, panel, button };
}

function drag(pressed: HTMLElement, grip: HTMLElement, toY: number): void {
  pressed.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerId: 1, clientY: 0 }));
  grip.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, pointerId: 1, clientY: toY }));
  grip.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, pointerId: 1, clientY: toY }));
}

beforeEach(() => {
  onDismiss = vi.fn();
});

afterEach(unmountViews);

describe("a phone sheet's drag", () => {
  it("dismisses past a quarter of the panel's height, and springs back short of it", () => {
    const { grip, panel } = mountSheet();
    drag(grip, grip, PANEL_HEIGHT * 0.2);
    expect(onDismiss).not.toHaveBeenCalled();
    expect(panel.style.transform).toBe("");

    drag(grip, grip, PANEL_HEIGHT * 0.3);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("leaves a press on a control in the head to the control", () => {
    const { grip, button } = mountSheet();
    drag(button, grip, PANEL_HEIGHT);
    expect(onDismiss).not.toHaveBeenCalled();
  });
});

function pressKey(key: string): void {
  document.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
}

describe("a phone sheet's Escape", () => {
  it("dismisses the sheet, leaves it to a menu open over it, and stops listening once the sheet is gone", () => {
    mountSheet();
    pressKey("Enter");
    expect(onDismiss).not.toHaveBeenCalled();

    const menu = document.body.appendChild(document.createElement("div"));
    menu.setAttribute("data-menu-part", "menu");
    pressKey("Escape");
    expect(onDismiss).not.toHaveBeenCalled();
    menu.remove();

    pressKey("Escape");
    expect(onDismiss).toHaveBeenCalledTimes(1);
    unmountViews();
    pressKey("Escape");
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
