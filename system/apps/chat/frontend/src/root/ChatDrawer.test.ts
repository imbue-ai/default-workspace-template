// @vitest-environment jsdom
/**
 * The phone layout's drawer and the list it hosts: every row keeps its right-click verbs behind a
 * kebab, which opens them without picking the row; the drawer goes on a scrim tap, Escape (unless a
 * modal over it takes the key), or a drag far enough to the left, and a shorter drag springs back
 * without the release picking a row.
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
import { scopeOfHandshake } from "@imbue/workspace-ui/src/element_reference";
import { ChatDrawer } from "./ChatDrawer";
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

function rail(): ChatRailAttrs {
  return {
    rows: ROWS,
    selectedChatId: "agent-1",
    isCompact: true,
    onPick,
    onNew: () => undefined,
    referenceScope: scopeOfHandshake(null),
    onDraftReference: () => undefined,
    isReferenceDraftAvailable: false,
  };
}

function mount(isCovered = false): HTMLElement {
  root = document.createElement("div");
  document.body.appendChild(root);
  m.mount(root, { view: () => m(ChatDrawer, { rail: rail(), isCovered, onDismiss }) });
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

beforeEach(() => {
  onPick = vi.fn();
  onDismiss = vi.fn();
  vi.clearAllMocks();
});

afterEach(() => {
  if (root !== null) {
    m.mount(root, null);
    root.remove();
    root = null;
  }
  document.body.innerHTML = "";
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

  it("goes on a tap on the scrim and on Escape", () => {
    mount();
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
