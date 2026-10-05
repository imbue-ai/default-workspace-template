// @vitest-environment jsdom
/**
 * The rail's width: dragged from its right edge within its bounds, kept in storage for the next
 * load, nudged by the arrow keys, and put back to the default by a double-click on the edge.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import { ChatRail } from "./ChatRail";
import { chatRailAttrsFixture } from "./chatRailAttrsFixture";
import { DEFAULT_RAIL_WIDTH_PX, MAX_RAIL_WIDTH_PX, MIN_RAIL_WIDTH_PX, initRailWidth, railWidth } from "./railWidth";

const STORAGE_KEY = "chat-root-rail-width";

let root: HTMLElement | null = null;

function mountRail(): { rail: HTMLElement; handle: HTMLElement } {
  root = document.createElement("div");
  document.body.appendChild(root);
  m.mount(root, {
    view: () => m(ChatRail, chatRailAttrsFixture()),
  });
  const rail = root.querySelector<HTMLElement>(".chat-rail");
  const handle = root.querySelector<HTMLElement>(".chat-rail-resize");
  if (rail === null || handle === null) throw new Error("no rail or no resize handle");
  handle.setPointerCapture = () => undefined;
  return { rail, handle };
}

function dragEdge(handle: HTMLElement, fromX: number, toX: number): void {
  const press = (type: string, clientX: number): void => {
    handle.dispatchEvent(new PointerEvent(type, { bubbles: true, pointerId: 1, button: 0, clientX, clientY: 300 }));
  };
  press("pointerdown", fromX);
  press("pointermove", (fromX + toX) / 2);
  press("pointermove", toX);
  press("pointerup", toX);
  m.redraw.sync();
}

beforeEach(() => {
  window.localStorage.clear();
  initRailWidth();
});

function unmountRail(): void {
  if (root === null) return;
  m.mount(root, null);
  root.remove();
  root = null;
}

afterEach(() => {
  unmountRail();
  window.localStorage.clear();
});

describe("rail width", () => {
  it("starts at the default and follows a drag of its edge, kept for the next load", () => {
    const { rail, handle } = mountRail();
    expect(rail.style.width).toBe(`${DEFAULT_RAIL_WIDTH_PX}px`);

    dragEdge(handle, 180, 300);
    expect(rail.style.width).toBe("300px");
    expect(handle.getAttribute("aria-valuenow")).toBe("300");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("300");

    initRailWidth();
    expect(railWidth()).toBe(300);
  });

  it("stops at its bounds, and a stored width out of them is brought back in", () => {
    const { rail, handle } = mountRail();
    dragEdge(handle, 240, 2000);
    expect(rail.style.width).toBe(`${MAX_RAIL_WIDTH_PX}px`);

    window.localStorage.setItem(STORAGE_KEY, "40");
    initRailWidth();
    expect(railWidth()).toBe(MIN_RAIL_WIDTH_PX);
    window.localStorage.setItem(STORAGE_KEY, "not a width");
    initRailWidth();
    expect(railWidth()).toBe(DEFAULT_RAIL_WIDTH_PX);
  });

  it("moves with the arrow keys and goes back to the default on a double-click", () => {
    const { rail, handle } = mountRail();
    handle.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    m.redraw.sync();
    expect(rail.style.width).toBe(`${DEFAULT_RAIL_WIDTH_PX + 16}px`);

    handle.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    m.redraw.sync();
    expect(rail.style.width).toBe(`${DEFAULT_RAIL_WIDTH_PX}px`);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("ends a drag whose rail goes mid-drag, so a later rail is not resized by a hover", () => {
    const first = mountRail();
    const press = (handle: HTMLElement, type: string, clientX: number): void => {
      handle.dispatchEvent(new PointerEvent(type, { bubbles: true, pointerId: 1, button: 0, clientX, clientY: 300 }));
    };
    press(first.handle, "pointerdown", 180);
    press(first.handle, "pointermove", 260);
    unmountRail();
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("260");

    const second = mountRail();
    press(second.handle, "pointermove", 400);
    m.redraw.sync();
    expect(second.rail.style.width).toBe("260px");
  });

  it("follows a width another root of this browser saves", () => {
    const { rail } = mountRail();
    const redraw = vi.spyOn(m, "redraw");
    window.localStorage.setItem(STORAGE_KEY, "260");
    window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
    expect(redraw).toHaveBeenCalled();
    m.redraw.sync();
    expect(rail.style.width).toBe("260px");
    redraw.mockRestore();
  });
});
