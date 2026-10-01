// @vitest-environment jsdom
/**
 * The phone layout's header: its kebab offers the chat on screen the verbs its row in the list offers, and, as
 * that row, nothing once the chat is being deleted.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

const verbs = vi.hoisted(() => ({
  stopChat: vi.fn(async (_chatId: string) => undefined),
  startChat: vi.fn(async (_chatId: string) => undefined),
  renameChat: vi.fn(async (_chatId: string, _title: string) => undefined),
  // A delete still under way: it never settles within the test.
  destroyChat: vi.fn((_chatId: string) => new Promise<undefined>(() => undefined)),
}));
vi.mock("./verbs", () => verbs);

import m from "mithril";
import { ChatHeader } from "./ChatHeader";
import type { ChatRow } from "./rows";

const ONLY_CHAT: ChatRow = {
  chatId: "agent-1",
  title: "Plan the launch",
  status: "idle",
  labels: {},
  agentIds: ["agent-1"],
  lastActiveMs: null,
  isProvisional: false,
};

let root: HTMLElement | null = null;

afterEach(() => {
  if (root !== null) {
    m.mount(root, null);
    root.remove();
    root = null;
  }
  vi.restoreAllMocks();
});

describe("the chat header", () => {
  it("offers no verbs for the chat on screen once its delete is under way", () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    root = document.createElement("div");
    document.body.appendChild(root);
    const context = { rows: [ONLY_CHAT], selectedChatId: ONLY_CHAT.chatId, onPick: () => undefined };
    m.mount(root, { view: () => m(ChatHeader, { row: ONLY_CHAT, context, onOpenList: () => undefined }) });

    root.querySelector<HTMLElement>("[data-chat-header-menu]")?.click();
    m.redraw.sync();
    document.querySelector<HTMLElement>('.chat-header-menu [data-menu-row="delete"]')?.click();
    m.redraw.sync();

    expect(verbs.destroyChat).toHaveBeenCalledTimes(1);
    // The only chat stays selected (there is no next one to move to), and a second Delete is not on offer.
    expect(root.querySelector("[data-chat-header-menu]")).toBeNull();
  });
});
