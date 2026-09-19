// @vitest-environment jsdom
/**
 * The chat root's inner frame pool: one frame per chat shown, the selected one visible and
 * the rest hidden, the frame shown longest ago destroyed past the bound (never the shown one),
 * and the pages driven through their embed API with the shell's handshake and their shown and
 * hidden states.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import type { ChatPageEmbedApi } from "../embedApi";
import { InnerFramePool, MAX_HELD_FRAMES } from "./framePool";

const HANDSHAKE: ShellHandshake = { clientId: "client-1", deviceKind: "", viewId: "", address: "", tabId: "" };

let container: HTMLElement;
let pool: InnerFramePool;
let now = 1_000;

function frames(): HTMLIFrameElement[] {
  return [...container.querySelectorAll("iframe")];
}

function frameOf(chatId: string): HTMLIFrameElement {
  const frame = frames().find((candidate) => candidate.dataset.chatId === chatId);
  if (frame === undefined) throw new Error(`no frame for ${chatId}`);
  return frame;
}

function visibleChatIds(): string[] {
  return frames()
    .filter((frame) => !frame.hidden)
    .map((frame) => frame.dataset.chatId ?? "");
}

/** Stand in for the page loading: install its embed API on the frame's window, then fire ``load``. */
function loadPage(chatId: string): { [K in keyof ChatPageEmbedApi]: ReturnType<typeof vi.fn> } {
  const api = { handshake: vi.fn(), shown: vi.fn(), hidden: vi.fn() };
  const frame = frameOf(chatId);
  const contentWindow = frame.contentWindow;
  if (contentWindow === null) throw new Error(`frame ${chatId} has no window`);
  contentWindow.chatPageEmbed = api;
  frame.dispatchEvent(new Event("load"));
  return api;
}

beforeEach(() => {
  now = 1_000;
  // Each show happens later than the last, so eviction has an order to go on.
  vi.spyOn(Date, "now").mockImplementation(() => (now += 1));
  container = document.createElement("div");
  document.body.appendChild(container);
  pool = new InnerFramePool(container);
});

afterEach(() => {
  container.remove();
  vi.restoreAllMocks();
});

describe("InnerFramePool", () => {
  it("makes one frame per chat at its page, shows the selected one and hides the rest", () => {
    pool.show("agent-a");
    pool.show("agent-b");

    expect(pool.heldChatIds()).toEqual(["agent-a", "agent-b"]);
    expect(frameOf("agent-a").getAttribute("src")).toBe("/agent-a");
    expect(frameOf("agent-b").getAttribute("src")).toBe("/agent-b");
    expect(visibleChatIds()).toEqual(["agent-b"]);

    pool.show("agent-a");
    expect(visibleChatIds()).toEqual(["agent-a"]);
    expect(pool.heldChatIds()).toEqual(["agent-a", "agent-b"]);

    pool.show(null);
    expect(visibleChatIds()).toEqual([]);
  });

  it("destroys the frame shown longest ago past the bound, never the shown one", () => {
    const chatIds = Array.from({ length: MAX_HELD_FRAMES + 1 }, (_, index) => `agent-${index}`);
    for (const chatId of chatIds) pool.show(chatId);
    expect(pool.heldChatIds()).toEqual(chatIds.slice(1));

    // Showing the oldest survivor again makes it the newest, so the next one to go is the one after it.
    pool.show(chatIds[1]);
    pool.show("agent-fresh");
    expect(pool.heldChatIds()).toEqual([chatIds[1], ...chatIds.slice(3), "agent-fresh"]);
    expect(visibleChatIds()).toEqual(["agent-fresh"]);
  });

  it("drops a destroyed chat's frame and knows its frames' windows", () => {
    pool.show("agent-a");
    pool.show("agent-b");
    const frameA = frameOf("agent-a");

    expect(pool.isInnerWindow(frameA.contentWindow)).toBe(true);
    expect(pool.isInnerWindow(window)).toBe(false);
    expect(pool.isInnerWindow(null)).toBe(false);

    pool.destroy("agent-a");
    expect(pool.heldChatIds()).toEqual(["agent-b"]);
    expect(frames().map((frame) => frame.dataset.chatId)).toEqual(["agent-b"]);
    expect(pool.isInnerWindow(frameA.contentWindow)).toBe(false);
  });

  it("hands a loaded page the handshake, now or when it arrives, and tells it shown and hidden", () => {
    pool.setRootShown(true);
    pool.show("agent-a");
    const pageA = loadPage("agent-a");
    expect(pageA.handshake).not.toHaveBeenCalled();
    expect(pageA.shown).toHaveBeenCalledTimes(1);

    pool.setHandshake(HANDSHAKE);
    expect(pageA.handshake).toHaveBeenCalledWith(HANDSHAKE);

    pool.show("agent-b");
    expect(pageA.hidden).toHaveBeenCalledTimes(1);
    const pageB = loadPage("agent-b");
    expect(pageB.handshake).toHaveBeenCalledWith(HANDSHAKE);
    expect(pageB.shown).toHaveBeenCalledTimes(1);

    // The shell hiding the root hides the shown page; showing it again shows the page.
    pool.setRootShown(false);
    expect(pageB.hidden).toHaveBeenCalledTimes(1);
    pool.setRootShown(true);
    expect(pageB.shown).toHaveBeenCalledTimes(2);
    expect(pageA.shown).toHaveBeenCalledTimes(1);
  });

  it("tells a page that loads while the root is hidden that it is hidden", () => {
    pool.setRootShown(false);
    pool.show("agent-a");
    const pageA = loadPage("agent-a");

    expect(pageA.hidden).toHaveBeenCalledTimes(1);
    expect(pageA.shown).not.toHaveBeenCalled();
  });
});
