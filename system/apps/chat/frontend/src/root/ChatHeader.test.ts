// @vitest-environment jsdom
/**
 * The phone layout's header: its kebab offers the chat on screen what a right-click on its row in the list does,
 * and, as that row, nothing once the chat is being deleted; with the drawer open over it, a rename is the row's alone.
 * Under a mouse the bar is denser than a touchscreen's.
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
import { ChatDrawer } from "./ChatDrawer";
import { ChatHeader } from "./ChatHeader";
import { chatRailAttrsFixture } from "./chatRailAttrsFixture";
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

// The rename case's chat: the delete case leaves its chat marked as being deleted for the rest of the file.
const RENAMED_CHAT: ChatRow = { ...ONLY_CHAT, chatId: "agent-2", agentIds: ["agent-2"] };

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
    const context = chatRailAttrsFixture({ rows: [ONLY_CHAT], selectedChatId: ONLY_CHAT.chatId });
    m.mount(root, {
      view: () =>
        m(ChatHeader, { row: ONLY_CHAT, context, isListOpen: false, onOpenList: () => undefined, isTouch: true }),
    });

    root.querySelector<HTMLElement>("[data-chat-header-menu]")?.click();
    m.redraw.sync();
    // What a right-click on the chat's row in the list offers: its verbs, then the reference rows.
    const menuRows = [...document.querySelectorAll<HTMLElement>(".chat-header-menu [data-menu-row]")];
    expect(menuRows.map((menuRow) => menuRow.getAttribute("data-menu-row"))).toEqual([
      "rename",
      "stop",
      "delete",
      "copy-reference",
      "explain-element",
      "modify-element",
    ]);
    document.querySelector<HTMLElement>('.chat-header-menu [data-menu-row="delete"]')?.click();
    m.redraw.sync();

    expect(verbs.destroyChat).toHaveBeenCalledTimes(1);
    // The only chat stays selected (there is no next one to move to), and a second Delete is not on offer.
    expect(root.querySelector("[data-chat-header-menu]")).toBeNull();
  });

  it("leaves a rename begun in the open drawer to the drawer's row", () => {
    root = document.createElement("div");
    document.body.appendChild(root);
    const rail = chatRailAttrsFixture({
      rows: [RENAMED_CHAT],
      selectedChatId: RENAMED_CHAT.chatId,
      isInDrawer: true,
      isTouch: true,
    });
    m.mount(root, {
      view: () => [
        m(ChatHeader, {
          row: RENAMED_CHAT,
          context: rail,
          isListOpen: true,
          onOpenList: () => undefined,
          isTouch: true,
        }),
        m(ChatDrawer, { rail, isCovered: false, onDismiss: () => undefined }),
      ],
    });

    root.querySelector<HTMLElement>(`[data-chat-row-menu="${RENAMED_CHAT.chatId}"]`)?.click();
    m.redraw.sync();
    document.querySelector<HTMLElement>('.chat-rail-menu [data-menu-row="rename"]')?.click();
    m.redraw.sync();

    const fields = root.querySelectorAll<HTMLInputElement>(".chat-rail-rename-input");
    expect(fields).toHaveLength(1);
    expect(fields[0].closest("[data-chat-drawer]")).not.toBeNull();
    expect(document.activeElement).toBe(fields[0]);
    expect(verbs.renameChat).not.toHaveBeenCalled();
  });

  it.each([
    { device: "a touchscreen", isTouch: true, height: "h-11", otherHeight: "h-9", buttonSize: "size-9" },
    { device: "a mouse", isTouch: false, height: "h-9", otherHeight: "h-11", buttonSize: "size-7" },
  ])("is $height with $buttonSize buttons under $device", ({ isTouch, height, otherHeight, buttonSize }) => {
    root = document.createElement("div");
    document.body.appendChild(root);
    const context = chatRailAttrsFixture();
    m.mount(root, {
      view: () => m(ChatHeader, { row: null, context, isListOpen: false, onOpenList: () => undefined, isTouch }),
    });

    const bar = root.querySelector<HTMLElement>(".chat-header");
    expect(bar?.classList.contains(height)).toBe(true);
    expect(bar?.classList.contains(otherHeight)).toBe(false);
    expect(bar?.querySelector(".chat-header-list")?.classList.contains(buttonSize)).toBe(true);
  });
});
