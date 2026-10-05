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
import { scopeOfHandshake } from "@imbue/workspace-ui/src/element_reference";
import { ChatDrawer } from "./ChatDrawer";
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
    const context = {
      rows: [ONLY_CHAT],
      selectedChatId: ONLY_CHAT.chatId,
      onPick: () => undefined,
      referenceScope: scopeOfHandshake(null),
      onDraftReference: () => undefined,
      isReferenceDraftAvailable: false,
    };
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
    const rail = {
      rows: [RENAMED_CHAT],
      selectedChatId: RENAMED_CHAT.chatId,
      isInDrawer: true,
      isTouch: true,
      onPick: () => undefined,
      onNew: () => undefined,
      referenceScope: scopeOfHandshake(null),
      onDraftReference: () => undefined,
      isReferenceDraftAvailable: false,
    };
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

  it("is a finger's height on a touchscreen and denser under a mouse", () => {
    const context = {
      rows: [],
      selectedChatId: null,
      onPick: () => undefined,
      referenceScope: scopeOfHandshake(null),
      onDraftReference: () => undefined,
      isReferenceDraftAvailable: false,
    };
    const header = (isTouch: boolean): HTMLElement => {
      root = document.createElement("div");
      document.body.appendChild(root);
      m.mount(root, {
        view: () => m(ChatHeader, { row: null, context, isListOpen: false, onOpenList: () => undefined, isTouch }),
      });
      const bar = root.querySelector<HTMLElement>(".chat-header");
      if (bar === null) throw new Error("no header");
      return bar;
    };

    const touchBar = header(true);
    expect(touchBar.className).toContain("h-11");
    expect(touchBar.querySelector(".chat-header-list")?.className).toContain("size-9");
    if (root !== null) {
      m.mount(root, null);
      root.remove();
    }

    const mouseBar = header(false);
    expect(mouseBar.className).toContain("h-9");
    expect(mouseBar.className).not.toContain("h-11");
    expect(mouseBar.querySelector(".chat-header-list")?.className).toContain("size-7");
  });
});
