// @vitest-environment jsdom
/**
 * The phone layout's header: its kebab offers the chat on screen the verbs its row in the list offers, and, as
 * that row, nothing once the chat is being deleted; with the drawer open over it, a rename is the row's alone.
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
    const context = { rows: [ONLY_CHAT], selectedChatId: ONLY_CHAT.chatId, onPick: () => undefined };
    m.mount(root, {
      view: () => m(ChatHeader, { row: ONLY_CHAT, context, isListOpen: false, onOpenList: () => undefined }),
    });

    root.querySelector<HTMLElement>("[data-chat-header-menu]")?.click();
    m.redraw.sync();
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
      isCompact: true,
      onPick: () => undefined,
      onNew: () => undefined,
      referenceScope: scopeOfHandshake(null),
      onDraftReference: () => undefined,
      isReferenceDraftAvailable: false,
    };
    m.mount(root, {
      view: () => [
        m(ChatHeader, { row: RENAMED_CHAT, context: rail, isListOpen: true, onOpenList: () => undefined }),
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
});
