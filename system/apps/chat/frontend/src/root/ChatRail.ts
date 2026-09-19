/**
 * The chat list down the left of the chat root: every chat the app lists, with its status,
 * the one the root shows marked, and starting a new one at the head. Picking a row shows
 * that chat in the root's inner frame.
 *
 * The rows are ordered and grouped by ``rows.ts``. The rail collapses to a strip of monograms
 * and status marks on a toggle (and by default on a phone), kept per browser. A row is
 * renameable in place (double-click, or the pencil under the pointer), and a right-click opens
 * a menu: rename, stop or restart, delete (which asks first). A stopped chat stays in the
 * list faded with a pause mark; one being deleted is crossed out until the list drops it.
 */

import m from "mithril";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { menuCardClass, menuDividerClass, menuRowClass } from "@imbue/workspace-ui/src/components/menu";
import { isUnread } from "./chatUnread";
import { destroyChat, renameChat, startChat, stopChat } from "./verbs";
import { isAgentStarted } from "./rows";
import type { ChatRow } from "./rows";

const COLLAPSED_STORAGE_KEY = "chat-root-rail-collapsed";

export interface ChatRailAttrs {
  /** The rows in display order. */
  rows: readonly ChatRow[];
  selectedChatId: string | null;
  /** Whether a phone-sized viewport is showing the root: the rail starts collapsed there. */
  isCompact: boolean;
  onPick: (chatId: string) => void;
  onNew: () => void;
}

/** The status each dot stands for: working is the accent and breathes; done (a finished turn
 *  the user has not looked at, see ``chatUnread``) is a green check; attention is amber; idle
 *  is a hollow grey ring; error is red; stopped wears a pause mark. */
const DOT_CLASS_BY_STATUS: Readonly<Record<string, string>> = {
  working: "chat-rail-dot--pulse bg-accent",
  attention: "bg-warning",
  error: "bg-danger",
  idle: "border border-faint bg-transparent",
};

function displayStatus(row: ChatRow): string {
  return row.status === "idle" && isUnread(row.chatId) ? "done" : row.status;
}

function monogramOf(row: ChatRow): string {
  const title = row.title.trim();
  return title === "" ? "?" : title.slice(0, 1).toUpperCase();
}

// ---------- per-browser rail state ----------

let collapsedChoice: boolean | null = null;

function isCollapsed(isCompact: boolean): boolean {
  if (collapsedChoice !== null) return collapsedChoice;
  try {
    const stored = window.localStorage.getItem(COLLAPSED_STORAGE_KEY);
    if (stored !== null) return stored === "true";
  } catch {
    // Storage denied: the default below stands.
  }
  return isCompact;
}

function setCollapsed(collapsed: boolean): void {
  collapsedChoice = collapsed;
  try {
    window.localStorage.setItem(COLLAPSED_STORAGE_KEY, collapsed ? "true" : "false");
  } catch {
    // Nothing to do: the rail works, it just will not be remembered.
  }
}

// ---------- renames, deletes, menus ----------

interface RenameState {
  chatId: string;
  draft: string;
  error: string | null;
}

let rename: RenameState | null = null;
const deletingChatIds = new Set<string>();

interface MenuState {
  chatId: string;
  x: number;
  y: number;
}

let menu: MenuState | null = null;

function beginRename(row: ChatRow): void {
  rename = { chatId: row.chatId, draft: row.title, error: null };
}

/** Keep the typed name: the field closes at once, and a refusal brings it back with the reason. */
function commitRename(row: ChatRow): void {
  if (rename === null || rename.chatId !== row.chatId) return;
  const title = rename.draft.trim();
  rename = null;
  m.redraw();
  if (title === "" || title === row.title) return;
  renameChat(row.chatId, title).catch((error: unknown) => {
    if (rename !== null) return;
    rename = { chatId: row.chatId, draft: title, error: error instanceof Error ? error.message : String(error) };
    m.redraw();
  });
}

function pruneDeleting(rows: readonly ChatRow[]): void {
  for (const chatId of deletingChatIds) {
    if (!rows.some((row) => row.chatId === chatId)) deletingChatIds.delete(chatId);
  }
}

function setRunningFromMenu(row: ChatRow, isRunning: boolean): void {
  menu = null;
  m.redraw();
  const verb = isRunning ? startChat : stopChat;
  verb(row.chatId).catch((error: unknown) => {
    alert(
      `Could not ${isRunning ? "restart" : "stop"} "${row.title}".\n\n${error instanceof Error ? error.message : String(error)}`,
    );
  });
}

/** Delete from the menu, after asking. When it is the chat the root shows, the root moves to
 *  the next one in the list first, so it is not left on a page whose chat is gone. */
function deleteFromMenu(attrs: ChatRailAttrs, row: ChatRow): void {
  menu = null;
  m.redraw();
  const isConfirmed = window.confirm(
    `Delete "${row.title}"?\n\nThis ends its agent and removes its conversation. It cannot be undone.`,
  );
  if (!isConfirmed) return;
  const next = attrs.rows.find(
    (candidate) => candidate.chatId !== row.chatId && !deletingChatIds.has(candidate.chatId),
  );
  deletingChatIds.add(row.chatId);
  m.redraw();
  if (row.chatId === attrs.selectedChatId && next !== undefined) attrs.onPick(next.chatId);
  destroyChat(row.chatId).catch((error: unknown) => {
    deletingChatIds.delete(row.chatId);
    m.redraw();
    alert(`Could not delete "${row.title}".\n\n${error instanceof Error ? error.message : String(error)}`);
  });
}

function keepMenuOnScreen(dom: Element): void {
  const element = dom as HTMLElement;
  const rect = element.getBoundingClientRect();
  const overflowX = rect.right - window.innerWidth;
  const overflowY = rect.bottom - window.innerHeight;
  if (overflowX > 0) element.style.left = `${Math.max(0, rect.left - overflowX)}px`;
  if (overflowY > 0) element.style.top = `${Math.max(0, rect.top - overflowY)}px`;
}

function rowMenu(attrs: ChatRailAttrs, row: ChatRow, state: MenuState): m.Vnode {
  const isStopped = row.status === "stopped";
  const rowClass = menuRowClass({ extra: "text-(length:--font-size-row) text-primary" });
  return m(
    "div",
    {
      class: `chat-rail-menu ${menuCardClass("fixed min-w-36")}`,
      style: `left: ${state.x}px; top: ${state.y}px`,
      role: "menu",
      oncreate: ({ dom }: m.VnodeDOM) => keepMenuOnScreen(dom),
      onclick: (event: MouseEvent) => event.stopPropagation(),
      oncontextmenu: (event: MouseEvent) => event.preventDefault(),
    },
    [
      m(
        "button",
        {
          type: "button",
          class: rowClass,
          role: "menuitem",
          "data-menu-item": "rename",
          onclick: () => {
            menu = null;
            beginRename(row);
          },
        },
        "Rename",
      ),
      m(
        "button",
        {
          type: "button",
          class: rowClass,
          role: "menuitem",
          "data-menu-item": isStopped ? "start" : "stop",
          onclick: () => setRunningFromMenu(row, isStopped),
        },
        isStopped ? "Restart chat" : "Stop chat",
      ),
      m("div", { class: menuDividerClass() }),
      m(
        "button",
        {
          type: "button",
          class: menuRowClass({ extra: "text-(length:--font-size-row) text-danger" }),
          role: "menuitem",
          "data-menu-item": "delete",
          onclick: () => deleteFromMenu(attrs, row),
        },
        "Delete chat",
      ),
    ],
  );
}

function closeMenuOnClickAway(): void {
  if (menu === null) return;
  menu = null;
  m.redraw();
}

function closeMenuOnEscape(event: KeyboardEvent): void {
  if (event.key !== "Escape" || menu === null) return;
  menu = null;
  m.redraw();
}

// ---------- marks ----------

/** A plus, drawn here because the shared icon set has no bare one. */
function plusGlyph(): m.Vnode {
  return m(
    "svg",
    {
      width: 16,
      height: 16,
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": 2,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": "true",
    },
    [m("path", { d: "M12 5v14" }), m("path", { d: "M5 12h14" })],
  );
}

/** The status mark beside a row: a dot in the status's colour, a check for done, a pause mark
 *  for a stopped chat (paused is what a stop is, since a message resumes it). */
function statusDot(row: ChatRow, extraClass: string): m.Vnode {
  const status = displayStatus(row);
  if (status === "done") {
    return m(
      "span",
      { class: `chat-rail-dot -mx-0.5 flex size-3 items-center text-success ${extraClass}`, "data-status": status },
      m.trust(icon("check", { size: 12, strokeWidth: 3 })),
    );
  }
  if (status === "stopped") {
    return m(
      "svg",
      {
        class: `chat-rail-dot size-2 rounded-full text-faint ${extraClass}`,
        "data-status": status,
        viewBox: "0 0 24 24",
        fill: "currentColor",
        "aria-hidden": "true",
      },
      [
        m("rect", { x: 4, y: 3, width: 5, height: 18, rx: 1 }),
        m("rect", { x: 15, y: 3, width: 5, height: 18, rx: 1 }),
      ],
    );
  }
  return m("span", {
    class: `chat-rail-dot size-2 rounded-full ${DOT_CLASS_BY_STATUS[status] ?? DOT_CLASS_BY_STATUS.idle} ${extraClass}`,
    "data-status": status,
  });
}

// ---------- the component ----------

export const ChatRail: m.Component<ChatRailAttrs> = {
  oncreate() {
    document.addEventListener("click", closeMenuOnClickAway);
    document.addEventListener("keydown", closeMenuOnEscape);
  },
  onremove() {
    document.removeEventListener("click", closeMenuOnClickAway);
    document.removeEventListener("keydown", closeMenuOnEscape);
    menu = null;
  },
  view({ attrs }) {
    const collapsed = isCollapsed(attrs.isCompact);
    pruneDeleting(attrs.rows);
    const menuRow = menu === null ? undefined : attrs.rows.find((row) => row.chatId === menu?.chatId);
    return m(
      "nav",
      {
        class: [
          "chat-rail relative flex h-full flex-none flex-col border-r border-default bg-surface",
          collapsed ? "chat-rail--collapsed w-13" : "w-60",
        ].join(" "),
        "aria-label": "Chats",
        "data-collapsed": collapsed ? "true" : "false",
      },
      [
        m(
          "div",
          {
            class: [
              "chat-rail-head flex flex-none gap-1 p-2",
              collapsed ? "flex-col items-stretch" : "items-center justify-between",
            ].join(" "),
          },
          [
            m(
              "button",
              {
                type: "button",
                class: [
                  "chat-rail-new flex flex-none items-center gap-2 rounded-md px-2 py-1.5",
                  "text-(length:--font-size-row) text-secondary hover:bg-fill-hover hover:text-primary",
                  collapsed ? "justify-center" : "",
                ].join(" "),
                "aria-label": "New chat",
                ...hoverTooltipAttrs(collapsed ? "New chat" : null, "right"),
                onclick: () => attrs.onNew(),
              },
              [plusGlyph(), collapsed ? null : m("span", "New chat")],
            ),
            m(
              "button",
              {
                type: "button",
                class:
                  "chat-rail-toggle flex flex-none items-center justify-center rounded-md p-1.5 text-faint hover:bg-fill-hover",
                "aria-label": collapsed ? "Show chat titles" : "Hide chat titles",
                ...hoverTooltipAttrs(collapsed ? "Show chat titles" : "Hide chat titles", "right"),
                onclick: () => {
                  setCollapsed(!collapsed);
                  m.redraw();
                },
              },
              m.trust(icon(collapsed ? "chevron-right" : "chevron-left", { size: 16 })),
            ),
          ],
        ),
        m(
          "div",
          { class: "chat-rail-list min-h-0 flex-1 overflow-y-auto px-2 pb-2" },
          attrs.rows.map((row) => railRow(attrs, row, collapsed)),
        ),
        menu !== null && menuRow !== undefined ? rowMenu(attrs, menuRow, menu) : null,
      ],
    );
  },
};

function railRow(attrs: ChatRailAttrs, row: ChatRow, collapsed: boolean): m.Vnode {
  const isSelected = row.chatId === attrs.selectedChatId;
  const isDeleting = deletingChatIds.has(row.chatId);
  if (!collapsed && rename !== null && rename.chatId === row.chatId) return renameRow(row, isSelected);
  const status = displayStatus(row);
  return m(
    "button",
    {
      key: row.chatId,
      type: "button",
      class: [
        "chat-rail-row group flex w-full items-center gap-2 rounded-md py-1.5 text-left",
        collapsed ? "justify-center px-1" : isAgentStarted(row) ? "chat-rail-row--nested pr-2 pl-5" : "px-2",
        isDeleting
          ? "chat-rail-row--deleting text-danger line-through opacity-50"
          : status === "done"
            ? `chat-rail-row--done font-semibold text-success ${isSelected ? "bg-fill-active" : "hover:bg-fill-hover"}`
            : isSelected
              ? "chat-rail-row--selected bg-fill-active text-primary"
              : "text-primary hover:bg-fill-hover",
        row.status === "stopped" ? "chat-rail-row--stopped opacity-50" : "",
      ].join(" "),
      "data-chat-id": row.chatId,
      "data-status": status,
      "aria-current": isSelected ? "true" : undefined,
      "aria-disabled": isDeleting ? "true" : undefined,
      ...hoverTooltipAttrs(collapsed ? row.title : null, "right"),
      onclick: () => {
        if (!isSelected) attrs.onPick(row.chatId);
      },
      ondblclick: collapsed || row.isProvisional ? undefined : () => beginRename(row),
      oncontextmenu: (event: MouseEvent) => {
        event.preventDefault();
        if (isDeleting || row.isProvisional) return;
        menu = { chatId: row.chatId, x: event.clientX, y: event.clientY };
      },
    },
    collapsed
      ? [
          m(
            "span",
            {
              class: "chat-rail-monogram relative flex size-7 items-center justify-center rounded-full bg-fill-hover",
            },
            [
              m("span", { class: "text-[10px] leading-none font-bold" }, monogramOf(row)),
              statusDot(row, "absolute -right-0.5 -bottom-0.5 ring-2 ring-surface"),
            ],
          ),
        ]
      : [
          statusDot(row, "flex-none"),
          m("span", { class: "chat-rail-title min-w-0 flex-1 truncate text-(length:--font-size-row)" }, row.title),
          row.isProvisional
            ? null
            : m(
                "span",
                {
                  class:
                    "chat-rail-rename flex-none rounded p-0.5 text-faint opacity-0 hover:bg-fill-hover hover:text-primary " +
                    "group-hover:opacity-100 focus-visible:opacity-100",
                  role: "button",
                  tabindex: 0,
                  "aria-label": "Rename chat",
                  onclick: (event: MouseEvent) => {
                    event.stopPropagation();
                    beginRename(row);
                  },
                  onkeydown: (event: KeyboardEvent) => {
                    if (event.key !== "Enter" && event.key !== " ") return;
                    event.preventDefault();
                    event.stopPropagation();
                    beginRename(row);
                  },
                },
                m.trust(icon("edit", { size: 13 })),
              ),
        ],
  );
}

function renameRow(row: ChatRow, isSelected: boolean): m.Vnode {
  const state = rename;
  if (state === null) throw new Error("renameRow rendered with no rename in progress");
  return m(
    "div",
    {
      key: row.chatId,
      class: [
        "chat-rail-row chat-rail-row--renaming flex w-full items-center gap-2 rounded-md py-1.5",
        isAgentStarted(row) ? "pr-2 pl-5" : "px-2",
        isSelected ? "bg-fill-active text-primary" : "text-primary",
      ].join(" "),
      "data-chat-id": row.chatId,
    },
    [
      statusDot(row, "flex-none"),
      m("input", {
        class: [
          "chat-rail-rename-input min-w-0 flex-1 rounded bg-surface px-1 text-(length:--font-size-row) text-primary outline-none ring-1",
          state.error === null ? "ring-accent" : "ring-danger",
        ].join(" "),
        type: "text",
        value: state.draft,
        "aria-label": "Chat name",
        "aria-invalid": state.error === null ? undefined : "true",
        title: state.error ?? undefined,
        maxlength: 256,
        oncreate: ({ dom }: m.VnodeDOM) => {
          const input = dom as HTMLInputElement;
          input.focus();
          input.select();
        },
        oninput: (event: InputEvent) => {
          if (rename === null) return;
          rename = { ...rename, draft: (event.target as HTMLInputElement).value, error: null };
        },
        onkeydown: (event: KeyboardEvent) => {
          event.stopPropagation();
          if (event.key === "Enter") {
            event.preventDefault();
            commitRename(row);
          } else if (event.key === "Escape") {
            event.preventDefault();
            rename = null;
          }
        },
        onblur: () => commitRename(row),
      }),
    ],
  );
}
