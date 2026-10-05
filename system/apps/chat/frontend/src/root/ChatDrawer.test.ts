// @vitest-environment jsdom
/**
 * The phone layout's drawer and the list it hosts: every row keeps its right-click verbs behind a
 * kebab, which opens them without picking the row; the drawer goes on a scrim tap, Escape (unless a
 * modal over it takes the key), or a drag far enough to the left, and a shorter drag springs back
 * without the release picking a row. Under a mouse the drawer holds the rail's own form of the list,
 * at the width the rail was dragged to.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const verbs = vi.hoisted(() => ({
  stopChat: vi.fn(async (_chatId: string) => undefined),
  startChat: vi.fn(async (_chatId: string) => undefined),
  renameChat: vi.fn(async (_chatId: string, _title: string) => undefined),
  destroyChat: vi.fn(async (_chatId: string) => undefined),
}));
vi.mock("./verbs", () => verbs);

import m from "mithril";
import { ChatDrawer } from "./ChatDrawer";
import { MIN_RAIL_WIDTH_PX, initRailWidth } from "./railWidth";
import { chatRailAttrsFixture } from "./chatRailAttrsFixture";
import type { ChatRailAttrs } from "./ChatRail";
import type { ChatRow } from "./rows";

function row(chatId: string, title: string, status = "idle"): ChatRow {
  return { chatId, title, status, labels: {}, agentIds: [chatId], lastActiveMs: null, isProvisional: false };
}

const ROWS = [row("agent-1", "Plan the launch"), row("agent-2", "Fix the tests", "stopped")];
const PANEL_WIDTH = 300;

let root: HTMLElement | null = null;
let onPick = vi.fn<(chatId: string) => void>();
let onDismiss = vi.fn<() => void>();

function rail(isTouch: boolean): ChatRailAttrs {
  return chatRailAttrsFixture({ rows: ROWS, selectedChatId: "agent-1", isInDrawer: true, isTouch, onPick });
}

function mount(isCovered = false, isTouch = true): HTMLElement {
  root = document.createElement("div");
  document.body.appendChild(root);
  m.mount(root, { view: () => m(ChatDrawer, { rail: rail(isTouch), isCovered, onDismiss }) });
  // jsdom lays nothing out: the panel is given the width a phone gives it.
  const panel = root.querySelector<HTMLElement>(".chat-drawer-panel");
  if (panel === null) throw new Error("no drawer panel");
  Object.defineProperty(panel, "offsetWidth", { value: PANEL_WIDTH });
  return panel;
}

function pointer(target: EventTarget, type: string, clientX: number): void {
  target.dispatchEvent(
    new PointerEvent(type, { bubbles: true, pointerId: 1, pointerType: "touch", clientX, clientY: 400 }),
  );
}

/** Drag from x=250 to `toX`, then release over the first row. */
function drag(panel: HTMLElement, toX: number): void {
  const firstRow = panel.querySelector<HTMLElement>(".chat-rail-row");
  if (firstRow === null) throw new Error("no row");
  pointer(firstRow, "pointerdown", 250);
  for (let x = 240; x >= toX; x -= 10) pointer(window, "pointermove", x);
  pointer(window, "pointerup", toX);
  firstRow.dispatchEvent(new MouseEvent("click", { bubbles: true }));
}

/** Mount the drawer under a mouse, the rail stored at `storedWidth` and, when given, laid out at `drawnWidth` (jsdom
 *  lays nothing out); `press` sends a mouse press's events to the list's resize edge. */
function mountUnderMouse({ storedWidth, drawnWidth }: { storedWidth?: number; drawnWidth?: number } = {}): {
  rail: HTMLElement;
  handle: HTMLElement;
  press: (type: string, clientX: number) => void;
} {
  if (storedWidth !== undefined) {
    window.localStorage.setItem("chat-root-rail-width", String(storedWidth));
    initRailWidth();
  }
  const panel = mount(false, false);
  const rail = panel.querySelector<HTMLElement>(".chat-rail");
  const handle = panel.querySelector<HTMLElement>(".chat-rail-resize");
  if (rail === null || handle === null) throw new Error("no rail or no resize handle");
  if (drawnWidth !== undefined) rail.getBoundingClientRect = () => new DOMRect(0, 0, drawnWidth, 800);
  handle.setPointerCapture = () => undefined;
  const press = (type: string, clientX: number): void => {
    handle.dispatchEvent(new PointerEvent(type, { bubbles: true, pointerId: 2, button: 0, clientX, clientY: 400 }));
  };
  return { rail, handle, press };
}

beforeEach(() => {
  onPick = vi.fn();
  onDismiss = vi.fn();
  vi.clearAllMocks();
  initRailWidth();
});

afterEach(() => {
  if (root !== null) {
    m.mount(root, null);
    root.remove();
    root = null;
  }
  document.body.innerHTML = "";
  window.localStorage.clear();
});

describe("ChatDrawer", () => {
  it("gives every row a kebab that opens its verbs without picking it, and runs the one chosen", () => {
    const panel = mount();
    expect(panel.querySelector(".chat-rail-new")?.getAttribute("aria-label")).toBe("New chat");

    panel.querySelector<HTMLElement>('[data-chat-row-menu="agent-2"]')?.click();
    m.redraw.sync();
    expect(onPick).not.toHaveBeenCalled();
    const menuRows = [...document.querySelectorAll<HTMLElement>(".chat-rail-menu [data-menu-row]")];
    // The stopped chat offers to restart rather than stop.
    expect(menuRows.map((menuRow) => menuRow.textContent)).toEqual(["Rename", "Restart chat", "Delete chat"]);

    document.querySelector<HTMLElement>('.chat-rail-menu [data-menu-row="start"]')?.click();
    expect(verbs.startChat).toHaveBeenCalledWith("agent-2");
    expect(verbs.stopChat).not.toHaveBeenCalled();
  });

  it("picks the chat on screen again when its row is tapped, which is what closes the drawer", () => {
    const panel = mount();
    panel.querySelector<HTMLElement>('.chat-rail-row[data-chat-id="agent-1"]')?.click();
    expect(onPick).toHaveBeenCalledWith("agent-1");
  });

  it("floats with a soft shadow over an undimmed chat, and goes on a tap beside it and on Escape", () => {
    const panel = mount();
    expect(panel.className).toContain("shadow-overlay");
    expect(root?.querySelector(".chat-drawer-scrim")?.className).toContain("bg-transparent");
    root?.querySelector<HTMLElement>(".chat-drawer-scrim")?.click();
    expect(onDismiss).toHaveBeenCalledTimes(1);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(onDismiss).toHaveBeenCalledTimes(2);
  });

  it("leaves Escape to a modal open over it", () => {
    mount(true);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("goes on a drag far enough to the left, and the release picks nothing", () => {
    const panel = mount();
    drag(panel, 100);
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(onPick).not.toHaveBeenCalled();
  });

  it("springs back from a short drag, and the release picks nothing", () => {
    const panel = mount();
    drag(panel, 200);
    expect(onDismiss).not.toHaveBeenCalled();
    expect(onPick).not.toHaveBeenCalled();
    expect(panel.style.transform).toBe("");
  });
});

describe("ChatDrawer under a mouse", () => {
  it("holds the rail's form of the list at the width the rail was dragged to", () => {
    window.localStorage.setItem("chat-root-rail-width", "320");
    initRailWidth();
    const panel = mount(false, false);

    expect(panel.className).not.toContain("w-[86%]");
    expect(panel.querySelector<HTMLElement>(".chat-rail")?.style.width).toBe("320px");
    // The rail's head and dense rows: "New chat" spelled out, a rename pencil rather than a kebab.
    expect(panel.querySelector(".chat-rail-new")?.textContent).toBe("New chat");
    expect(panel.querySelector(".chat-rail-row")?.className).not.toContain("min-h-11");
    expect(panel.querySelector("[data-chat-row-menu]")).toBeNull();
    expect(panel.querySelector(".chat-rail-rename")).not.toBeNull();
  });

  it("picks the chat on screen again when its row is clicked, which is what closes the drawer", () => {
    const panel = mount(false, false);
    panel.querySelector<HTMLElement>('.chat-rail-row[data-chat-id="agent-1"]')?.click();
    expect(onPick).toHaveBeenCalledWith("agent-1");
  });

  it("resizes from the list's edge without the press dragging the drawer away", () => {
    const { rail, press } = mountUnderMouse();
    press("pointerdown", 240);
    press("pointermove", 0);
    press("pointerup", 0);
    pointer(window, "pointermove", 0);
    pointer(window, "pointerup", 0);
    m.redraw.sync();

    expect(onDismiss).not.toHaveBeenCalled();
    expect(rail.style.width).toBe(`${MIN_RAIL_WIDTH_PX}px`);
  });

  it("drags from the width it draws when the window holds the list narrower than its width", () => {
    const { rail, press } = mountUnderMouse({ storedWidth: 480, drawnWidth: 340 });
    press("pointerdown", 340);
    press("pointermove", 320);
    press("pointerup", 320);
    m.redraw.sync();

    expect(rail.style.width).toBe("320px");
  });

  it("steps an arrow key from the width it draws when the window holds the list narrower than its width", () => {
    const { rail, handle } = mountUnderMouse({ storedWidth: 480, drawnWidth: 340 });
    handle.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    m.redraw.sync();

    expect(rail.style.width).toBe("324px");
  });
});
