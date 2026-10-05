/**
 * The chat root's header in the phone layout: a list button that opens the chats' drawer, the
 * title of the chat on screen, and a kebab offering that chat's verbs, the same rows its row in
 * the list offers. A rename from here is typed in place of the title; while the drawer is open, the
 * row there holds the field instead. On a touchscreen the bar is a finger's height; under a mouse
 * it is as dense as the rest of the desktop's chrome.
 */

import m from "mithril";
import { createMenu } from "@imbue/workspace-ui/src/components/menu";
import { kebabGlyph, listGlyph } from "../glyphs";
import { BAR_ICON_BUTTON_CLASS, isDeleting, isRenaming, renameField, rowMenuRows } from "./ChatRail";
import type { RowMenuContext } from "./ChatRail";
import type { ChatRow } from "./rows";

export interface ChatHeaderAttrs {
  /** The chat on screen, or null with none selected. */
  row: ChatRow | null;
  /** What the kebab's verbs need of the root, as the list's own menu does. */
  context: RowMenuContext;
  /** Whether the drawer is open over the header. */
  isListOpen: boolean;
  onOpenList: () => void;
  /** Whether the bar takes the finger's size, on a touchscreen. */
  isTouch: boolean;
}

/** The bar's buttons under a mouse, smaller than the touchscreen's ``BAR_ICON_BUTTON_CLASS``. */
const DENSE_BAR_ICON_BUTTON_CLASS =
  "flex size-7 flex-none items-center justify-center rounded-md text-primary hover:bg-fill-hover";

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
      const buttonClass = attrs.isTouch ? BAR_ICON_BUTTON_CLASS : DENSE_BAR_ICON_BUTTON_CLASS;
      return m(
        "header",
        {
          class: [
            "chat-header flex flex-none items-center gap-1 border-b border-default bg-page",
            attrs.isTouch ? "h-11 pr-2 pl-1.5" : "h-9 pr-1 pl-1",
          ].join(" "),
        },
        [
          m(
            "button",
            {
              type: "button",
              class: `chat-header-list ${buttonClass}`,
              "aria-label": "Chats",
              "data-chat-header-list": "",
              onclick: () => attrs.onOpenList(),
            },
            listGlyph(attrs.isTouch ? 22 : 18),
          ),
          row !== null && !attrs.isListOpen && isRenaming(row.chatId)
            ? m("span", { class: "ml-1 flex min-w-0 flex-1" }, renameField(row))
            : m(
                "span",
                {
                  class:
                    "chat-header-title ml-1 min-w-0 flex-1 truncate font-semibold " +
                    (attrs.isTouch ? "text-(length:--font-size-body) " : "text-(length:--font-size-row) ") +
                    (isRowDeleting ? "text-danger line-through opacity-50" : "text-primary"),
                },
                row?.title ?? "Chats",
              ),
          hasVerbs
            ? m(
                "button",
                {
                  type: "button",
                  class: `chat-header-menu-button ${buttonClass}`,
                  "aria-label": "Chat actions",
                  "data-chat-header-menu": "",
                  ...menu.triggerAttrs(),
                },
                kebabGlyph(attrs.isTouch ? 18 : 16),
              )
            : null,
          hasVerbs ? menu.view(rowMenuRows(attrs.context, row)) : null,
        ],
      );
    },
  };
}
