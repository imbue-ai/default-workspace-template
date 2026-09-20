// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "../testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { appRecord, windowRecord } from "../testing/records";
import { Window } from "./Window";
import type { WindowAttrs } from "./Window";

afterEach(unmountViews);

function render(overrides: Partial<WindowAttrs> = {}): HTMLElement {
  const attrs: WindowAttrs = {
    window: windowRecord("win-1", "docs", "/?doc=1", { title: "Plan" }),
    app: appRecord("docs"),
    title: "Plan",
    rect: { x: 50, y: 60, width: 640, height: 480 },
    state: "NORMAL",
    stackIndex: 1,
    isFocused: true,
    isCompact: false,
    isTouch: false,
    isMenuOpen: false,
    isShielded: false,
    isPlacedHere: true,
    onStartApp: null,
    onRaise: vi.fn(),
    onControl: vi.fn(),
    onToggleMaximize: vi.fn(),
    ...overrides,
  };
  const root = mountView(() => m(Window, attrs));
  return root.querySelector('[data-window-id="win-1"]') as HTMLElement;
}

describe("Window", () => {
  it("carries the contract's selectors, its rectangle, and its stacking order", () => {
    const element = render();
    expect(element.getAttribute("data-window-state")).toBe("NORMAL");
    expect(element.getAttribute("data-minimized")).toBe("false");
    expect(element.getAttribute("data-focused")).toBe("true");
    expect(element.style.left).toBe("50px");
    expect(element.style.width).toBe("640px");
    expect(element.style.zIndex).toBe("4");
    expect(element.querySelector("[data-drag-handle]")).not.toBeNull();
    expect(element.querySelector("[data-window-content]")).not.toBeNull();
    expect(element.querySelectorAll("[data-resize-edge]")).toHaveLength(8);
    expect(element.querySelector(".window-title")?.textContent).toBe("Plan");
    expect(element.getAttribute("data-pinned")).toBe("false");
    expect(element.querySelector('[data-window-control="close"]')).not.toBeNull();
  });

  it("a pinned window is marked and has no close control", () => {
    const element = render({ window: windowRecord("win-1", "docs", "/", { is_pinned: true }) });
    expect(element.getAttribute("data-pinned")).toBe("true");
    expect(element.querySelector('[data-window-control="close"]')).toBeNull();
    expect(element.querySelector('[data-window-control="minimize"]')).not.toBeNull();
  });

  it("lays a shield over the content when asked, whose press raises an unfocused window and not a focused one", () => {
    const onRaise = vi.fn();
    const unfocused = render({ isFocused: false, isShielded: true, onRaise });
    const shield = unfocused.querySelector("[data-window-shield]") as HTMLElement;
    expect(shield).not.toBeNull();
    shield.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    expect(onRaise).toHaveBeenCalledTimes(1);
    unmountViews();
    expect(render({ isFocused: true, isShielded: false }).querySelector("[data-window-shield]")).toBeNull();
    unmountViews();
    const focusedUnderMenu = render({ isFocused: true, isShielded: true, onRaise });
    const focusedShield = focusedUnderMenu.querySelector("[data-window-shield]") as HTMLElement;
    expect(focusedShield).not.toBeNull();
    focusedShield.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    expect(onRaise).toHaveBeenCalledTimes(1);
  });

  it("offers the controls in the title bar's order and reports each", () => {
    const onControl = vi.fn();
    const element = render({ onControl });
    const controls = [...element.querySelectorAll("[data-window-control]")].map((control) =>
      control.getAttribute("data-window-control"),
    );
    expect(controls).toEqual(["menu", "minimize", "maximize", "close"]);
    (element.querySelector('[data-window-control="close"]') as HTMLElement).click();
    expect(onControl).toHaveBeenCalledWith("close", expect.anything());
  });

  it("shows Restore when maximized, and hides the maximize controls and resize edges in compact mode", () => {
    const maximized = render({ state: "MAXIMIZED" });
    expect(maximized.querySelector('[data-window-control="restore"]')).not.toBeNull();
    unmountViews();
    const compact = render({ isCompact: true, state: "MAXIMIZED" });
    expect(compact.querySelector('[data-window-control="restore"]')).toBeNull();
    expect(compact.querySelector('[data-window-control="maximize"]')).toBeNull();
    expect(compact.querySelectorAll("[data-resize-edge]")).toHaveLength(0);
    unmountViews();
    expect(render({ isTouch: true }).querySelectorAll("[data-resize-edge]")).toHaveLength(0);
  });

  it("shows a placeholder for a stopped app, with Start where the workspace can start it", () => {
    const onStartApp = vi.fn();
    const element = render({ app: appRecord("docs", { is_running: false }), onStartApp });
    const placeholder = element.querySelector("[data-stopped-app]") as HTMLElement;
    expect(placeholder.textContent).toContain("Docs");
    (placeholder.querySelector("button") as HTMLElement).click();
    expect(onStartApp).toHaveBeenCalled();
    unmountViews();
    expect(
      render({ app: appRecord("docs", { is_running: false }), onStartApp: null }).querySelector(
        "[data-stopped-app] button",
      ),
    ).toBeNull();
  });

  it("says a window settling on another client's open is starting elsewhere", () => {
    const element = render({
      window: windowRecord("win-1", "docs", "/new", { is_settling: true }),
      isPlacedHere: false,
    });
    expect(element.querySelector("[data-settling]")).not.toBeNull();
    unmountViews();
    expect(
      render({
        window: windowRecord("win-1", "docs", "/new", { is_settling: true }),
        isPlacedHere: true,
      }).querySelector("[data-settling]"),
    ).toBeNull();
  });

  it("a double click on the title bar toggles maximize, a click on a control does not", () => {
    const onToggleMaximize = vi.fn();
    const element = render({ onToggleMaximize });
    (element.querySelector("[data-drag-handle]") as HTMLElement).dispatchEvent(
      new MouseEvent("dblclick", { bubbles: true }),
    );
    expect(onToggleMaximize).toHaveBeenCalledTimes(1);
    (element.querySelector('[data-window-control="close"]') as HTMLElement).dispatchEvent(
      new MouseEvent("dblclick", { bubbles: true }),
    );
    expect(onToggleMaximize).toHaveBeenCalledTimes(1);
  });
});
