// @vitest-environment jsdom
/**
 * The rail's width: dragged from its right edge within its bounds, kept in storage for the next
 * load, nudged by the arrow keys, and put back to the default by a double-click on the edge.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import m from "mithril";
import { scopeOfHandshake } from "@imbue/workspace-ui/src/element_reference";
import { ChatRail } from "./ChatRail";
import { DEFAULT_RAIL_WIDTH_PX, MAX_RAIL_WIDTH_PX, initRailWidth, railWidth } from "./railWidth";

const STORAGE_KEY = "chat-root-rail-width";

let root: HTMLElement | null = null;

function mountRail(): { rail: HTMLElement; handle: HTMLElement } {
  root = document.createElement("div");
  document.body.appendChild(root);
  m.mount(root, {
    view: () =>
      m(ChatRail, {
        rows: [],
        selectedChatId: null,
        isInDrawer: false,
        isTouch: false,
        onPick: () => undefined,
        onNew: () => undefined,
        referenceScope: scopeOfHandshake(null),
        onDraftReference: () => undefined,
        isReferenceDraftAvailable: false,
      }),
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

afterEach(() => {
  if (root !== null) {
    m.mount(root, null);
    root.remove();
    root = null;
  }
  window.localStorage.clear();
});

describe("rail width", () => {
  it("starts at the default and follows a drag of its edge, kept for the next load", () => {
    const { rail, handle } = mountRail();
    expect(rail.style.width).toBe(`${DEFAULT_RAIL_WIDTH_PX}px`);

    dragEdge(handle, 240, 300);
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
    expect(railWidth()).toBe(180);
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
});
