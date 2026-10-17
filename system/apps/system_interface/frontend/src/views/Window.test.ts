// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
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
    isTouch: false,
    isMenuOpen: false,
    sizeMenuTrigger: {},
    isShielded: false,
    isStopped: false,
    immersive: null,
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

  it("shows Fullscreen and Restore when maximized, and no resize edges on a touch screen", () => {
    const onControl = vi.fn();
    const maximized = render({ state: "MAXIMIZED", onControl });
    const controls = [...maximized.querySelectorAll("[data-window-control]")].map((control) =>
      control.getAttribute("data-window-control"),
    );
    expect(controls).toEqual(["refresh", "menu", "minimize", "fullscreen", "restore", "close"]);
    (maximized.querySelector('[data-window-control="fullscreen"]') as HTMLElement).click();
    expect(onControl).toHaveBeenCalledWith("fullscreen", expect.anything());
    unmountViews();
    expect(render({ isTouch: true }).querySelectorAll("[data-resize-edge]")).toHaveLength(0);
  });

  it("a fullscreen window has no title bar, no handles and no corners, and a strip at its top edge that exits", () => {
    const onControl = vi.fn();
    const element = render({ state: "FULLSCREEN", onControl });
    expect(element.getAttribute("data-window-state")).toBe("FULLSCREEN");
    expect(element.querySelector("[data-drag-handle]")).toBeNull();
    expect(element.querySelectorAll("[data-window-control]")).toHaveLength(0);
    expect(element.querySelectorAll("[data-resize-edge]")).toHaveLength(0);
    const frame = element.querySelector("[data-window-frame]") as HTMLElement;
    expect(frame.className).not.toContain("rounded");
    expect(frame.className).not.toContain("shadow");
    expect(element.querySelector("[data-window-content]")).not.toBeNull();
    const strip = element.querySelector("[data-fullscreen-strip]") as HTMLElement;
    expect(strip).not.toBeNull();
    expect(strip.className).toContain("h-(--desk-resize-edge)");
    // The chrome never said it could take the window immersive: the control is not there and nothing is said.
    expect(strip.querySelector('[data-fullscreen-control="immersive"]')).toBeNull();
    expect(strip.querySelector("[data-fullscreen-hint]")?.textContent).toBe("");
    (strip.querySelector('[data-fullscreen-control="exit-fullscreen"]') as HTMLElement).click();
    expect(onControl).toHaveBeenCalledWith("fullscreen", expect.anything());
  });

  it("the strip offers Immersive where the chrome can, and Leave immersive with the lock's word while it is on", () => {
    const onEnter = vi.fn();
    const onLeave = vi.fn();
    const off = render({
      state: "FULLSCREEN",
      immersive: { isOn: false, isKeyboardLocked: false, onEnter, onLeave },
    });
    expect(off.querySelector("[data-fullscreen-hint]")?.textContent).toBe("Press Immersive to send every key");
    (off.querySelector('[data-fullscreen-control="immersive"]') as HTMLElement).click();
    expect(onEnter).toHaveBeenCalledTimes(1);
    expect(off.querySelector('[data-fullscreen-control="leave-immersive"]')).toBeNull();
    unmountViews();
    const locked = render({
      state: "FULLSCREEN",
      immersive: { isOn: true, isKeyboardLocked: true, onEnter, onLeave },
    });
    expect(locked.querySelector("[data-fullscreen-hint]")?.textContent).toBe("Every key reaches this window");
    expect(locked.querySelector('[data-fullscreen-control="immersive"]')).toBeNull();
    (locked.querySelector('[data-fullscreen-control="leave-immersive"]') as HTMLElement).click();
    expect(onLeave).toHaveBeenCalledTimes(1);
    unmountViews();
    const unlocked = render({
      state: "FULLSCREEN",
      immersive: { isOn: true, isKeyboardLocked: false, onEnter, onLeave },
    });
    expect(unlocked.querySelector("[data-fullscreen-hint]")?.textContent).toBe("Immersive, without the keyboard lock");
  });

  it("shows a placeholder, and no page, for an app the caller says is stopped", () => {
    const element = render({ app: appRecord("docs", { is_running: false, program: "" }), isStopped: true });
    const placeholder = element.querySelector("[data-stopped-app]") as HTMLElement;
    expect(placeholder.textContent).toContain("Docs");
    expect(placeholder.textContent).toContain("not running (managed outside the workspace)");
    expect(placeholder.querySelector("button")).toBeNull();
    unmountViews();
    // A stopped app the shell wakes on a request keeps its page: the parker answers it.
    expect(
      render({ app: appRecord("docs", { is_running: false }), isStopped: false }).querySelector("[data-stopped-app]"),
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
