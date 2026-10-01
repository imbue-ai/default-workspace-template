/**
 * The chat root's header in the phone layout: a list button that opens the chats' drawer, the
 * title of the chat on screen, and a kebab offering that chat's verbs, the same rows its row in
 * the list offers. A rename from here is typed in place of the title.
 */

import m from "mithril";
import { createMenu } from "@imbue/workspace-ui/src/components/menu";
import { kebabGlyph, listGlyph } from "../glyphs";
import { isDeleting, isRenaming, renameField, rowMenuRows } from "./ChatRail";
import type { RowMenuContext } from "./ChatRail";
import type { ChatRow } from "./rows";

export interface ChatHeaderAttrs {
  /** The chat on screen, or null with none selected. */
  row: ChatRow | null;
  /** What the kebab's verbs need of the root, as the list's own menu does. */
  context: RowMenuContext;
  onOpenList: () => void;
}

const ICON_BUTTON_CLASS =
  "flex size-9 flex-none items-center justify-center rounded-lg text-primary hover:bg-fill-hover";

export function ChatHeader(): m.Component<ChatHeaderAttrs> {
  const menu = createMenu({
    placement: "below",
    align: "end",
    role: "menu",
    minWidth: 144,
    extraClass: "chat-header-menu",
  });

  return {
    onremove() {
      menu.dispose();
    },
    view({ attrs }) {
      const { row } = attrs;
      // A chat being deleted offers nothing more, as its row in the list does not.
      const isRowDeleting = row !== null && isDeleting(row.chatId);
      const hasVerbs = row !== null && !row.isProvisional && !isRowDeleting;
      return m(
        "header",
        { class: "chat-header flex h-11 flex-none items-center gap-1 border-b border-default bg-page pr-2 pl-1.5" },
        [
          m(
            "button",
            {
              type: "button",
              class: `chat-header-list ${ICON_BUTTON_CLASS}`,
              "aria-label": "Chats",
              "data-chat-header-list": "",
              onclick: () => attrs.onOpenList(),
            },
            listGlyph(),
          ),
          row !== null && isRenaming(row.chatId)
            ? m("span", { class: "ml-1 flex min-w-0 flex-1" }, renameField(row))
            : m(
                "span",
                {
                  class:
                    "chat-header-title ml-1 min-w-0 flex-1 truncate text-(length:--font-size-body) font-semibold " +
                    (isRowDeleting ? "text-danger line-through opacity-50" : "text-primary"),
                },
                row?.title ?? "Chats",
              ),
          hasVerbs
            ? m(
                "button",
                {
                  type: "button",
                  class: `chat-header-menu-button ${ICON_BUTTON_CLASS}`,
                  "aria-label": "Chat actions",
                  "data-chat-header-menu": "",
                  ...menu.triggerAttrs(),
                },
                kebabGlyph(18),
              )
            : null,
          hasVerbs ? menu.view(rowMenuRows(attrs.context, row)) : null,
        ],
      );
    },
  };
}
