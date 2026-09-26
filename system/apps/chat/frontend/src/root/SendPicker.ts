/**
 * The send picker (post-launch-paths plan section 3.6): what the chat root opens over its list for a held intake
 * whose chat is the user's to choose (the launcher's "Send to chat..." with several chats). A typeahead over every
 * chat's title with one row highlighted: Enter or a click applies the intake on that chat (the root's business,
 * reported through ``onPick``), Escape or a press outside drops it. A chat that is not an agent yet cannot take a
 * message, so it is not offered. The title says what the pick does with the text: sends it, or drafts it.
 */

import m from "mithril";
import { Modal } from "@imbue/workspace-ui/src/components/Modal";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { menuRowClass } from "@imbue/workspace-ui/src/components/menu";
import { matchesQuery } from "@imbue/workspace-ui/src/search";
import type { ChatRow } from "./rows";

const PICKER_WIDTH_PX = 440;
const SEND_TITLE = "Send to which chat?";
const DRAFT_TITLE = "Draft into which chat?";
const NO_MATCH_MESSAGE = "No chat matches";

export interface SendPickerAttrs {
  /** The chats in display order; provisional ones are left out. */
  rows: readonly ChatRow[];
  /** The text to send or draft, shown so the reader knows what goes where. */
  text: string;
  /** Whether the pick drafts the text into the chosen chat's composer rather than sending it. */
  isDraft: boolean;
  onPick: (chatId: string) => void;
  onDismiss: () => void;
}

/** The rows a query keeps: the chats that are agents, whose title the query matches. */
export function pickableRows(rows: readonly ChatRow[], query: string): ChatRow[] {
  return rows.filter((row) => !row.isProvisional && matchesQuery(query, row.title));
}

export function SendPicker(): m.Component<SendPickerAttrs> {
  let query = "";
  let highlightIndex = 0;

  function highlighted(rows: readonly ChatRow[]): ChatRow | undefined {
    return rows[Math.min(highlightIndex, rows.length - 1)];
  }

  return {
    view(vnode) {
      const { rows, text, isDraft, onPick, onDismiss } = vnode.attrs;
      const title = isDraft ? DRAFT_TITLE : SEND_TITLE;
      const shown = pickableRows(rows, query);
      const highlightedRow = highlighted(shown);
      return m(
        Modal,
        {
          onDismiss,
          onEscape: onDismiss,
          width: PICKER_WIDTH_PX,
          card: { role: "dialog", "aria-modal": "true", "aria-label": title, "data-send-picker": "" },
          title,
        },
        [
          m("p", { class: "send-picker-text type-helper m-0 mb-3 truncate text-secondary" }, `“${text}”`),
          m("input", {
            type: "text",
            "data-send-picker-search": "",
            "aria-label": title,
            placeholder: "Type to narrow the chats",
            value: query,
            class: inputClass(),
            oncreate: (created: m.VnodeDOM) => (created.dom as HTMLInputElement).focus(),
            oninput: (event: InputEvent) => {
              query = (event.target as HTMLInputElement).value;
              highlightIndex = 0;
            },
            onkeydown: (event: KeyboardEvent) => {
              if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                if (shown.length === 0) return;
                const step = event.key === "ArrowDown" ? 1 : -1;
                highlightIndex =
                  (((Math.min(highlightIndex, shown.length - 1) + step) % shown.length) + shown.length) % shown.length;
                return;
              }
              if (event.key === "Enter" && highlightedRow !== undefined) {
                event.preventDefault();
                onPick(highlightedRow.chatId);
              }
            },
          }),
          m(
            "div",
            { class: "send-picker-rows mt-2 max-h-[40vh] overflow-y-auto", role: "listbox" },
            shown.length === 0
              ? m("p", { class: "send-picker-no-match type-helper m-0 px-3 py-2 text-faint" }, NO_MATCH_MESSAGE)
              : shown.map((row) =>
                  m(
                    "button",
                    {
                      key: row.chatId,
                      type: "button",
                      role: "option",
                      "data-send-target": row.chatId,
                      "aria-selected": row === highlightedRow ? "true" : "false",
                      class:
                        menuRowClass({ extra: "rounded-md text-(length:--font-size-row) text-primary" }) +
                        (row === highlightedRow ? " bg-fill-active" : ""),
                      onpointerenter: () => {
                        highlightIndex = shown.indexOf(row);
                      },
                      onclick: () => onPick(row.chatId),
                    },
                    m("span", { class: "min-w-0 flex-1 truncate" }, row.title),
                  ),
                ),
          ),
        ],
      );
    },
  };
}
