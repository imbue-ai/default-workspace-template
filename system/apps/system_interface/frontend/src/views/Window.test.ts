// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { appRecord, windowRecord } from "../testing/records";
import { Window } from "./Window";
import type { WindowAttrs } from "./Window";
import { STANDARD_CHROME } from "../model/themes";

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
    isTouch: false,
    isMenuOpen: false,
    sizeMenuTrigger: {},
    isShielded: false,
    stopped: null,
    isAppGone: false,
    onRaise: vi.fn(),
    onControl: vi.fn(),
    onToggleMaximize: vi.fn(),
    onClose: vi.fn(),
    chrome: STANDARD_CHROME,
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

  it("a pinned window is marked and keeps its close control", () => {
    const element = render({ window: windowRecord("win-1", "docs", "/", { is_pinned: true }) });
    expect(element.getAttribute("data-pinned")).toBe("true");
    expect(element.querySelector('[data-window-control="close"]')).not.toBeNull();
    expect(element.querySelector('[data-window-control="minimize"]')).not.toBeNull();
  });

  it("keeps the resize handles outside the clipped frame, so they can overhang its edge", () => {
    const element = render();
    const frame = element.querySelector("[data-window-frame]") as HTMLElement;
    expect(frame.classList.contains("overflow-hidden")).toBe(true);
    expect(element.classList.contains("overflow-hidden")).toBe(false);
    expect(frame.querySelector("[data-window-content]")).not.toBeNull();
    for (const handle of element.querySelectorAll("[data-resize-edge]")) {
      expect(handle.parentElement).toBe(element);
      expect(handle.className).toContain("-(--desk-resize-overhang)");
    }
    expect(element.querySelector('[data-resize-edge="se"]')?.className).toContain("size-(--desk-resize-corner)");
    expect(element.querySelector('[data-resize-edge="n"]')?.className).toContain("h-(--desk-resize-edge)");
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
    expect(controls).toEqual(["refresh", "menu", "minimize", "maximize", "close"]);
    (element.querySelector('[data-window-control="close"]') as HTMLElement).click();
    expect(onControl).toHaveBeenCalledWith("close", expect.anything());
  });

  it("shows Restore when maximized, and no resize edges on a touch screen", () => {
    const maximized = render({ state: "MAXIMIZED" });
    expect(maximized.querySelector('[data-window-control="restore"]')).not.toBeNull();
    unmountViews();
    expect(render({ isTouch: true }).querySelectorAll("[data-resize-edge]")).toHaveLength(0);
  });

  it("shows what the caller says of a stopped app in place of its page, with the next step's button", () => {
    const askAgent = vi.fn();
    const element = render({
      app: appRecord("docs", { is_running: false, program: "" }),
      stopped: {
        message: "Docs isn't running.",
        details: null,
        actions: [{ label: "Ask an agent to start it", run: askAgent }],
        isReportable: false,
      },
    });
    const placeholder = element.querySelector("[data-stopped-app]") as HTMLElement;
    expect(placeholder.querySelector(".stopped-app-notice-message")?.textContent).toBe("Docs isn't running.");
    (placeholder.querySelector("button") as HTMLButtonElement).click();
    expect(askAgent).toHaveBeenCalledTimes(1);
    unmountViews();
    // A stopped app the shell wakes on a request keeps its page: the parker answers it.
    expect(
      render({ app: appRecord("docs", { is_running: false }), stopped: null }).querySelector("[data-stopped-app]"),
    ).toBeNull();
  });

  it("says a window's app is no longer part of the workspace, offers to close it, and drops Refresh", () => {
    const onClose = vi.fn();
    const element = render({ app: undefined, isAppGone: true, onClose });
    const placeholder = element.querySelector("[data-gone-app]") as HTMLElement;
    expect(placeholder.querySelector(".gone-app-notice-message")?.textContent).toBe(
      "This app is no longer part of this workspace.",
    );
    (placeholder.querySelector("button") as HTMLButtonElement).click();
    expect(onClose).toHaveBeenCalledTimes(1);
    const controls = [...element.querySelectorAll("[data-window-control]")].map((control) =>
      control.getAttribute("data-window-control"),
    );
    expect(controls).toEqual(["menu", "minimize", "maximize", "close"]);
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
