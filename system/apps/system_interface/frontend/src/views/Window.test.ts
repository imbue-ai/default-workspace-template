// @vitest-environment jsdom
import "../testing/dom";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { appRecord, windowRecord } from "../testing/records";
import { Window } from "./Window";
import type { WindowAttrs } from "./Window";

let root: HTMLElement | null = null;

afterEach(() => {
  if (root !== null) {
    m.mount(root, null);
    root.remove();
    root = null;
  }
});

function render(overrides: Partial<WindowAttrs> = {}): HTMLElement {
  root = document.createElement("div");
  document.body.appendChild(root);
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
    hasPage: true,
    onStartApp: null,
    onRaise: vi.fn(),
    onControl: vi.fn(),
    onToggleMaximize: vi.fn(),
    ...overrides,
  };
  m.mount(root, { view: () => m(Window, attrs) });
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
  });

  it("lays a shield over an unfocused window's content whose press raises it, and none over the focused one", () => {
    const onRaise = vi.fn();
    const unfocused = render({ isFocused: false, onRaise });
    const shield = unfocused.querySelector("[data-window-shield]") as HTMLElement;
    expect(shield).not.toBeNull();
    shield.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    expect(onRaise).toHaveBeenCalled();
    m.mount(root as HTMLElement, null);
    expect(render({ isFocused: true }).querySelector("[data-window-shield]")).toBeNull();
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
    m.mount(root as HTMLElement, null);
    const compact = render({ isCompact: true, state: "MAXIMIZED" });
    expect(compact.querySelector('[data-window-control="restore"]')).toBeNull();
    expect(compact.querySelector('[data-window-control="maximize"]')).toBeNull();
    expect(compact.querySelectorAll("[data-resize-edge]")).toHaveLength(0);
    m.mount(root as HTMLElement, null);
    expect(render({ isTouch: true }).querySelectorAll("[data-resize-edge]")).toHaveLength(0);
  });

  it("shows a placeholder for a stopped app, with Start where the workspace can start it", () => {
    const onStartApp = vi.fn();
    const element = render({ app: appRecord("docs", { is_running: false }), onStartApp });
    const placeholder = element.querySelector("[data-stopped-app]") as HTMLElement;
    expect(placeholder.textContent).toContain("Docs");
    (placeholder.querySelector("button") as HTMLElement).click();
    expect(onStartApp).toHaveBeenCalled();
    m.mount(root as HTMLElement, null);
    expect(
      render({ app: appRecord("docs", { is_running: false }), onStartApp: null }).querySelector(
        "[data-stopped-app] button",
      ),
    ).toBeNull();
  });

  it("says a window settling on another client's open is starting elsewhere", () => {
    const element = render({ window: windowRecord("win-1", "docs", "/new", { is_settling: true }), hasPage: false });
    expect(element.querySelector("[data-settling]")).not.toBeNull();
    m.mount(root as HTMLElement, null);
    expect(
      render({ window: windowRecord("win-1", "docs", "/new", { is_settling: true }), hasPage: true }).querySelector(
        "[data-settling]",
      ),
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
