/**
 * The phone's windows sheet (plan-phone-interface.md): every window of every desktop as one list, icon and title
 * only, in the order ``reducers/phone`` gives. A tap shows the window; each row's X closes it for everyone (the
 * pinned window has none, since it is never closed) and its kebab opens the window's menu. "Close all" closes
 * every other window after the owner confirms; past ``WINDOW_SEARCH_THRESHOLD`` windows a field filters the rows
 * by title and app.
 */

import m from "mithril";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { matchesQuery } from "@imbue/workspace-ui/src/search";
import type { AvatarState } from "../../reducers/desktopState";
import type { PhoneWindowRow } from "../../reducers/phone";
import { AvatarImage } from "../AvatarImage";
import { appGlyph, glyph } from "../glyphs";
import { Sheet } from "./Sheet";

const ROW_GLYPH_SIZE = 32;
const ACTION_GLYPH_SIZE = 16;
/** Past this many windows the sheet offers a field to find one. */
export const WINDOW_SEARCH_THRESHOLD = 6;

export interface WindowsSheetAttrs {
  readonly rows: readonly PhoneWindowRow[];
  readonly shownWindowId: string | null;
  readonly avatar: AvatarState;
  readonly query: string;
  readonly onQuery: (query: string) => void;
  readonly onShow: (windowId: string) => void;
  readonly onClose: (windowId: string) => void;
  readonly onMenu: (windowId: string, target: HTMLElement) => void;
  readonly onCloseAll: () => void;
  readonly onDismiss: () => void;
}

function rowIcon(row: PhoneWindowRow, avatar: AvatarState): m.Children {
  const isAvatar = row.window.is_pinned && row.app?.pin?.style === "avatar";
  return m(
    "span",
    {
      class:
        "flex size-(--desk-phone-row-icon) shrink-0 items-center justify-center overflow-hidden [&>svg]:size-full " +
        (isAvatar ? "rounded-full bg-surface" : "rounded-(--desk-icon-radius)"),
    },
    isAvatar
      ? m(AvatarImage, {
          design: avatar.design,
          defaultDesign: avatar.defaultDesign,
          mood: avatar.status.mood,
          class: "size-full object-contain",
        })
      : m.trust(appGlyph(row.app, ROW_GLYPH_SIZE)),
  );
}

const ROW_ACTION_CLASS =
  "flex size-9 shrink-0 items-center justify-center rounded-full text-faint active:bg-fill-active";

export const WindowsSheet: m.Component<WindowsSheetAttrs> = {
  view(vnode) {
    const { rows, shownWindowId, avatar, query, onQuery, onShow, onClose, onMenu, onCloseAll, onDismiss } =
      vnode.attrs;
    const closable = rows.filter((row) => !row.window.is_pinned).length;
    const shown = rows.filter((row) =>
      matchesQuery(query.trim(), row.title, row.app?.display_name ?? "", row.window.app),
    );
    return m(
      Sheet,
      {
        name: "windows",
        onDismiss,
        head: m("div", { class: "flex items-center justify-between px-4 pt-1.5 pb-2.5" }, [
          m("h2", { class: "m-0 text-lg font-bold text-primary" }, "Windows"),
          m(
            "button",
            {
              type: "button",
              "data-phone-close-all": "",
              disabled: closable === 0,
              class: "py-1.5 text-sm font-semibold text-danger disabled:text-faint",
              onclick: onCloseAll,
            },
            "Close all",
          ),
        ]),
      },
      [
        rows.length > WINDOW_SEARCH_THRESHOLD
          ? m(
              "label",
              {
                class:
                  "mx-3 mb-1.5 flex h-11 shrink-0 items-center gap-2.5 rounded-xl border border-strong bg-surface px-3 " +
                  "text-faint",
              },
              [
                m.trust(icon("search", { size: ACTION_GLYPH_SIZE + 2 })),
                m("input", {
                  "data-phone-window-search": "",
                  type: "search",
                  placeholder: "Find a window",
                  value: query,
                  class: "min-w-0 flex-1 bg-transparent text-(length:--font-size-body) text-primary outline-none",
                  oninput: (event: InputEvent) => onQuery((event.target as HTMLInputElement).value),
                }),
              ],
            )
          : null,
        m(
          "div",
          { class: "phone-window-list min-h-0 flex-1 overflow-y-auto pt-1 pb-(--desk-phone-safe-bottom)" },
          shown.map((row) =>
            m(
              "div",
              {
                key: row.window.id,
                "data-phone-window-row": row.window.id,
                "data-current": row.window.id === shownWindowId ? "true" : "false",
                role: "button",
                tabindex: 0,
                class:
                  "phone-window-row mx-2 flex h-(--desk-phone-row-height) cursor-pointer items-center gap-3 " +
                  "rounded-xl pr-1 pl-2 active:bg-fill-hover " +
                  (row.window.id === shownWindowId ? "bg-fill-active" : ""),
                onclick: () => onShow(row.window.id),
              },
              [
                rowIcon(row, avatar),
                m("span", { class: "min-w-0 flex-1 truncate text-(length:--font-size-body) text-primary" }, row.title),
                m(
                  "button",
                  {
                    type: "button",
                    "data-phone-window-menu": row.window.id,
                    "aria-label": `Menu of ${row.title}`,
                    class: ROW_ACTION_CLASS,
                    onclick: (event: MouseEvent) => {
                      event.stopPropagation();
                      onMenu(row.window.id, event.currentTarget as HTMLElement);
                    },
                  },
                  m.trust(glyph("kebab", ACTION_GLYPH_SIZE)),
                ),
                row.window.is_pinned
                  ? // Where the X would be, so the kebabs line up down the list.
                    m("span", { class: "size-9 shrink-0" })
                  : m(
                      "button",
                      {
                        type: "button",
                        "data-phone-window-close": row.window.id,
                        "aria-label": `Close ${row.title}`,
                        class: ROW_ACTION_CLASS,
                        onclick: (event: MouseEvent) => {
                          event.stopPropagation();
                          onClose(row.window.id);
                        },
                      },
                      m.trust(icon("close", { size: ACTION_GLYPH_SIZE })),
                    ),
              ],
            ),
          ),
        ),
      ],
    );
  },
};
