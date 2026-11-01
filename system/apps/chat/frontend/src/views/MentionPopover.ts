/**
 * The popover typing ``@`` opens in a composer (the multiplayer spec, section 7.2): the handle table filtered by
 * what follows the ``@``, people first, then agents, then chats; Enter or a click inserts ``@<handle> ``. The
 * composer owns the draft (where the ``@`` is, what follows it, which row is selected) and the key handling; this is
 * the rendering, plus the pure helpers that read and rewrite the draft in the composer's text.
 */

import m from "mithril";
import type { MentionHandle } from "../models/Mentions";
import { peopleGlyph } from "../glyphs";

/** Where a mention is being typed: the ``@`` at ``start``, ``query`` after it, and the row the keys selected. */
export interface MentionDraft {
  start: number;
  query: string;
  index: number;
}

/** How many rows the popover shows at most: past that, typing narrows it. */
export const MENTION_ROW_CAP = 8;

// An ``@`` that starts a mention: not inside a word or another mention, followed by handle characters up to the caret.
const DRAFT_PATTERN = /(?:^|[^\w@])@([A-Za-z0-9._-]*)$/;

/** The mention being typed at ``caret`` in ``text``, keeping the selected row of ``previous`` when it is the same
 *  mention; null when the caret does not follow an ``@`` and its handle characters. */
export function mentionDraftAt(text: string, caret: number, previous: MentionDraft | null): MentionDraft | null {
  const match = DRAFT_PATTERN.exec(text.slice(0, caret));
  if (match === null) return null;
  const query = match[1];
  const start = caret - query.length - 1;
  return { start, query, index: previous !== null && previous.start === start ? previous.index : 0 };
}

/** ``text`` with the draft replaced by ``@<handle> ``, and where the caret lands after it. */
export function insertMention(text: string, draft: MentionDraft, handle: string): { text: string; caret: number } {
  const inserted = `@${handle} `;
  const caret = draft.start + inserted.length;
  return { text: text.slice(0, draft.start) + inserted + text.slice(draft.start + 1 + draft.query.length), caret };
}

export interface MentionPopoverAttrs {
  rows: readonly MentionHandle[];
  selectedIndex: number;
  onPick: (handle: MentionHandle) => void;
  onHover: (index: number) => void;
}

function kindLabel(handle: MentionHandle): string {
  switch (handle.kind) {
    case "chat":
      return "chat";
    case "workspace_agent":
    case "visiting_agent":
      return "agent";
    case "invited":
      return "invited";
    default:
      return "";
  }
}

/** The popover, above the composer's box; nothing while no row matches. */
export function mentionPopover(attrs: MentionPopoverAttrs): m.Vnode | null {
  if (attrs.rows.length === 0) return null;
  return m(
    "div",
    {
      class:
        "message-input-mentions absolute bottom-full left-0 z-20 mb-1 max-h-64 w-72 max-w-full overflow-y-auto " +
        "rounded-lg border border-default bg-surface py-1 shadow-lg",
      role: "listbox",
      "data-mention-popover": "",
      // The textarea keeps the focus (and the caret) through a click on a row.
      onmousedown: (event: MouseEvent) => event.preventDefault(),
    },
    attrs.rows.map((handle, index) =>
      m(
        "div",
        {
          key: handle.handle,
          role: "option",
          "aria-selected": index === attrs.selectedIndex ? "true" : "false",
          "data-mention-handle": handle.handle,
          class:
            "flex cursor-pointer items-center gap-2 px-3 py-1.5 text-(length:--font-size-body) " +
            (index === attrs.selectedIndex ? "bg-fill-active text-primary" : "text-primary hover:bg-fill-hover"),
          onclick: () => attrs.onPick(handle),
          onmouseenter: () => attrs.onHover(index),
        },
        [
          handle.kind === "chat"
            ? m("span", { class: "flex w-4 shrink-0 justify-center text-faint" }, peopleGlyph(13))
            : null,
          m("span", { class: "min-w-0 flex-1 truncate" }, [
            m("span", { class: "font-medium" }, `@${handle.handle}`),
            handle.display_name !== handle.handle
              ? m("span", { class: "ml-2 text-secondary" }, handle.display_name)
              : null,
          ]),
          kindLabel(handle)
            ? m("span", { class: "shrink-0 text-(length:--font-size-helper) text-faint" }, kindLabel(handle))
            : null,
        ],
      ),
    ),
  );
}
