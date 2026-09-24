// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import type { ChatRow } from "./rows";
import { SendPicker, pickableRows } from "./SendPicker";

function row(chatId: string, title: string, isProvisional = false): ChatRow {
  return { chatId, title, status: "idle", labels: {}, agentIds: [chatId], lastActiveMs: null, isProvisional };
}

const ROWS = [row("agent-1", "Plan the launch"), row("agent-2", "Fix the tests"), row("agent-3", "New chat", true)];

let root: HTMLElement | null = null;

function mount(attrs: { onPick: (chatId: string) => void; onDismiss: () => void }): HTMLElement {
  root = document.createElement("div");
  document.body.appendChild(root);
  m.mount(root, { view: () => m(SendPicker, { rows: ROWS, text: "hello there", ...attrs }) });
  return root;
}

afterEach(() => {
  if (root !== null) {
    m.mount(root, null);
    root.remove();
    root = null;
  }
});

describe("pickableRows", () => {
  it("offers the chats that are agents, narrowed by title", () => {
    expect(pickableRows(ROWS, "").map((candidate) => candidate.chatId)).toEqual(["agent-1", "agent-2"]);
    expect(pickableRows(ROWS, "tests").map((candidate) => candidate.chatId)).toEqual(["agent-2"]);
    expect(pickableRows(ROWS, "zzz")).toEqual([]);
  });
});

describe("SendPicker", () => {
  it("shows the text and the chats, highlights the first, and picks on a click", () => {
    const onPick = vi.fn();
    const picker = mount({ onPick, onDismiss: vi.fn() });
    expect(picker.querySelector(".send-picker-text")!.textContent).toBe("“hello there”");
    const targets = Array.from(picker.querySelectorAll("[data-send-target]"));
    expect(targets.map((target) => target.getAttribute("data-send-target"))).toEqual(["agent-1", "agent-2"]);
    expect(targets[0].getAttribute("aria-selected")).toBe("true");
    (targets[1] as HTMLElement).click();
    expect(onPick).toHaveBeenCalledWith("agent-2");
  });

  it("narrows on typing, moves the highlight with the arrows, and Enter picks the highlight", () => {
    const onPick = vi.fn();
    const picker = mount({ onPick, onDismiss: vi.fn() });
    const input = picker.querySelector<HTMLInputElement>("[data-send-picker-search]")!;
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    m.redraw.sync();
    expect(picker.querySelector('[data-send-target="agent-2"]')!.getAttribute("aria-selected")).toBe("true");
    input.value = "plan";
    input.dispatchEvent(new InputEvent("input", { bubbles: true }));
    m.redraw.sync();
    expect(picker.querySelectorAll("[data-send-target]")).toHaveLength(1);
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(onPick).toHaveBeenCalledWith("agent-1");
    input.value = "zzz";
    input.dispatchEvent(new InputEvent("input", { bubbles: true }));
    m.redraw.sync();
    expect(picker.querySelector(".send-picker-no-match")).not.toBeNull();
  });

  it("dismisses on Escape", () => {
    const onDismiss = vi.fn();
    mount({ onPick: vi.fn(), onDismiss });
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
