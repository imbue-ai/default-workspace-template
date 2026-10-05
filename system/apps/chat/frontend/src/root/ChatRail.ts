/**
 * The chat list down the left of the chat root: every chat the app lists, with its status,
 * the one the root shows marked, and starting a new one at the head. Picking a row shows
 * that chat in the root's inner frame.
 *
 * The rows are ordered and grouped by ``rows.ts``. A row is renameable in place (double-click,
 * or the pencil under the pointer), and a right-click opens a menu: rename, stop or restart,
 * delete (which asks first). A stopped chat stays in the list faded with a pause mark; one
 * being deleted is crossed out until the list drops it.
 *
 * The list's right edge drags to resize it (``railWidth``); a double-click there puts back the
 * default width.
 *
 * In the phone layout the same list is the drawer's (``ChatDrawer``). Under a mouse the drawer
 * holds the list just as the rail draws it, at the rail's width. On a touchscreen it takes the
 * finger's form: "New chat" is a plus in its header, and each row carries a kebab offering the
 * right-click menu's verbs, since nothing on a phone right-clicks. The phone header's kebab
 * offers the whole right-click menu, verbs and reference rows, for the chat on screen
 * (``rowMenuRows``, ``referenceRowsFor``, ``renameField``).
 */

import m from "mithril";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { createMenu, type MenuRow } from "@imbue/workspace-ui/src/components/menu";
import { elementReferenceRows, targetOfEvent } from "@imbue/workspace-ui/src/context_menu_rows";
import { describeElement, type ReferenceClick, type ReferenceScope } from "@imbue/workspace-ui/src/element_reference";
import { anchorForPoint } from "@imbue/workspace-ui/src/menu-position";
import { kebabGlyph, plusGlyph } from "../glyphs";
import { isUnread } from "./chatUnread";
import { destroyChat, renameChat, startChat, stopChat } from "./verbs";
import { isAgentStarted } from "./rows";
import {
  DEFAULT_RAIL_WIDTH_PX,
  MAX_RAIL_WIDTH_PX,
  MIN_RAIL_WIDTH_PX,
  railWidth,
  saveRailWidth,
  setRailWidth,
} from "./railWidth";
import type { ChatRow } from "./rows";

export interface ChatRailAttrs {
  /** The rows in display order. */
  rows: readonly ChatRow[];
  selectedChatId: string | null;
  /** Whether the root draws its phone layout, where this list is the drawer's. */
  isInDrawer: boolean;
  /** Whether the list takes the finger's form: the drawer on a touchscreen. */
  isTouch: boolean;
  onPick: (chatId: string) => void;
  onNew: () => void;
  /** The scope a reference to a row carries (the root's handshake). */
  referenceScope: ReferenceScope;
  /** Where a row's "Explain..." drafts (element-reference-menu plan section 4.4). */
  onDraftReference: (text: string) => void;
  /** Whether such a draft has somewhere to go; the rows grey otherwise. */
  isReferenceDraftAvailable: boolean;
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

// Renames, deletes, and the row menu

interface RenameState {
  chatId: string;
  draft: string;
  error: string | null;
}

let rename: RenameState | null = null;
const deletingChatIds = new Set<string>();

// The row whose menu is up, or null. Which row it belongs to is this file's; opening, placing,
// dismissing and closing are the component's, and its `onClose` keeps the two in step.
let menuChatId: string | null = null;
// The reference rows for the element the open menu was opened on, built at the right-click; none
// for a menu opened from a row's kebab.
let menuReferenceRows: readonly MenuRow[] = [];

const railMenu = createMenu({
  placement: "below",
  role: "menu",
  // What the old hand-built card set as `min-w-36`.
  minWidth: 144,
  extraClass: "chat-rail-menu",
  onClose: () => {
    menuChatId = null;
    menuReferenceRows = [];
  },
});

function beginRename(row: ChatRow): void {
  rename = { chatId: row.chatId, draft: row.title, error: null };
}

/** Whether ``chatId`` is being deleted: its delete was asked for and has not failed. */
export function isDeleting(chatId: string): boolean {
  return deletingChatIds.has(chatId);
}

/** Whether ``chatId`` is being renamed. */
export function isRenaming(chatId: string): boolean {
  return rename !== null && rename.chatId === chatId;
}

/** Keep the typed name: the field closes at once, and a refusal brings it back with the reason. */
function commitRename(row: ChatRow): void {
  if (rename === null || rename.chatId !== row.chatId) return;
  const title = rename.draft.trim();
  rename = null;
  m.redraw();
  if (title === "" || title === row.title) return;
  renameChat(row.chatId, title).catch((error: unknown) => {
    if (rename !== null) {
      // Another row's rename has the field: the refusal cannot be shown there, so it is logged.
      console.warn(`Could not rename chat ${row.chatId}`, error);
      return;
    }
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
  const verb = isRunning ? startChat : stopChat;
  verb(row.chatId).catch((error: unknown) => {
    alert(
      `Could not ${isRunning ? "restart" : "stop"} "${row.title}".\n\n${error instanceof Error ? error.message : String(error)}`,
    );
  });
}

/** What a row's menu needs of the root: the rows, the one shown, and how to show another. */
export type RowMenuContext = Pick<ChatRailAttrs, "rows" | "selectedChatId" | "onPick">;

/** What a reference to an element of the list needs of the root, as its right-click menu does. */
export type RowReferenceContext = Pick<
  ChatRailAttrs,
  "referenceScope" | "onDraftReference" | "isReferenceDraftAvailable"
>;

/** The reference rows of a menu about ``element``, opened at ``click``: what a right-click on it offers. */
export function referenceRowsFor(attrs: RowReferenceContext, element: Element, click: ReferenceClick): MenuRow[] {
  const reference = describeElement(element, click, attrs.referenceScope);
  return elementReferenceRows(reference, attrs.onDraftReference, attrs.isReferenceDraftAvailable);
}

/** Delete from the menu, after asking. When it is the chat the root shows, the root moves to
 *  the next one in the list first, so it is not left on a page whose chat is gone. */
function deleteFromMenu(attrs: RowMenuContext, row: ChatRow): void {
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

/** The rows a chat's menu offers, ending with ``referenceRows``: the reference rows for the element right-clicked,
 *  and none for a menu opened from a kebab. */
export function rowMenuRows(attrs: RowMenuContext, row: ChatRow, referenceRows: readonly MenuRow[] = []): MenuRow[] {
  const isStopped = row.status === "stopped";
  return [
    { kind: "action", key: "rename", label: "Rename", onSelect: () => beginRename(row) },
    {
      kind: "action",
      key: isStopped ? "start" : "stop",
      label: isStopped ? "Restart chat" : "Stop chat",
      onSelect: () => setRunningFromMenu(row, isStopped),
    },
    { kind: "divider" },
    {
      kind: "action",
      key: "delete",
      label: "Delete chat",
      tone: "danger",
      onSelect: () => deleteFromMenu(attrs, row),
    },
    ...(referenceRows.length === 0 ? [] : [{ kind: "divider" } as MenuRow, ...referenceRows]),
  ];
}

/** The reference rows for a right-click on a row's element, built as the menu opens. */
function referenceRowsForEvent(attrs: ChatRailAttrs, event: MouseEvent): MenuRow[] {
  const target = targetOfEvent(event, document);
  return referenceRowsFor(attrs, target.element, target.click);
}

// The status marks

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

// The component

/** Open a row's menu from its kebab: the same verbs as a right-click, hung off the kebab. */
function openRowMenu(row: ChatRow, kebab: HTMLElement): void {
  menuChatId = row.chatId;
  menuReferenceRows = [];
  railMenu.open(kebab);
}

export const ChatRail: m.Component<ChatRailAttrs> = {
  onremove() {
    // A menu still open when the rail unmounts would keep its Escape listener on the window.
    railMenu.dispose();
  },
  view({ attrs }) {
    pruneDeleting(attrs.rows);
    const menuRow = menuChatId === null ? undefined : attrs.rows.find((row) => row.chatId === menuChatId);
    return m(
      "nav",
      {
        class: [
          "chat-rail relative flex h-full flex-none flex-col bg-surface",
          attrs.isTouch ? "w-full" : "border-r border-default",
          // The drawer leaves a strip of the scrim to click away on, however wide the list was dragged.
          attrs.isInDrawer && !attrs.isTouch ? "max-w-[calc(100vw-3rem)]" : "",
        ].join(" "),
        style: attrs.isTouch ? undefined : { width: `${railWidth()}px` },
        "aria-label": "Chats",
      },
      [
        attrs.isTouch ? drawerHead(attrs) : railHead(attrs),
        m(
          "div",
          {
            // A touch's allowed gestures are read from the touched row up to the first scrolling ancestor, this
            // list, so the drawer's own pan-y must be repeated here for a sideways drag over the rows to reach it.
            class: `chat-rail-list min-h-0 flex-1 overflow-x-hidden overflow-y-auto px-2 pb-2${attrs.isTouch ? " touch-pan-y" : ""}`,
          },
          attrs.rows.map((row) => railRow(attrs, row)),
        ),
        attrs.isTouch ? null : resizeHandle(),
        menuRow === undefined ? null : railMenu.view(rowMenuRows(attrs, menuRow, menuReferenceRows)),
      ],
    );
  },
};

/** How far one arrow key moves the list's edge. */
const RESIZE_KEY_STEP_PX = 16;

// The drag on the list's edge being followed, from its pointerdown to its release; null between drags.
let resizeDrag: { pointerId: number; startX: number; startWidth: number } | null = null;

function endResize(event: PointerEvent): void {
  if (resizeDrag === null || event.pointerId !== resizeDrag.pointerId) return;
  resizeDrag = null;
  saveRailWidth();
}

/** The width the list is drawn at, which a drag or an arrow key moves from. The drawer can draw the list narrower than
 *  its width, held back from the window's edge; starting from what is drawn, the edge moves from the first step. */
function drawnRailWidth(handle: HTMLElement): number {
  const drawnWidth = handle.parentElement?.getBoundingClientRect().width ?? 0;
  return drawnWidth > 0 ? Math.min(railWidth(), drawnWidth) : railWidth();
}

/** The list's right edge, dragged to resize it. The pointer is captured for the drag, so it keeps coming here while
 *  it crosses the chat's frame; the drawer's own drag never sees the press. */
function resizeHandle(): m.Vnode {
  return m("div", {
    class:
      "chat-rail-resize absolute inset-y-0 -right-1 z-10 w-2 cursor-col-resize touch-none " +
      "after:absolute after:inset-y-0 after:left-[3px] after:w-0.5 after:bg-accent after:opacity-0 " +
      "after:transition-opacity hover:after:opacity-100 focus-visible:after:opacity-100 focus-visible:outline-none",
    role: "separator",
    tabindex: 0,
    "aria-orientation": "vertical",
    "aria-label": "Resize the chat list",
    "aria-valuenow": railWidth(),
    "aria-valuemin": MIN_RAIL_WIDTH_PX,
    "aria-valuemax": MAX_RAIL_WIDTH_PX,
    onpointerdown: (event: PointerEvent) => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      const handle = event.currentTarget as HTMLElement;
      handle.setPointerCapture(event.pointerId);
      resizeDrag = { pointerId: event.pointerId, startX: event.clientX, startWidth: drawnRailWidth(handle) };
    },
    onpointermove: (event: PointerEvent & { redraw?: boolean }) => {
      if (resizeDrag === null || event.pointerId !== resizeDrag.pointerId) {
        event.redraw = false;
        return;
      }
      setRailWidth(resizeDrag.startWidth + event.clientX - resizeDrag.startX);
    },
    onpointerup: endResize,
    onpointercancel: endResize,
    ondblclick: () => {
      setRailWidth(DEFAULT_RAIL_WIDTH_PX);
      saveRailWidth();
    },
    onkeydown: (event: KeyboardEvent) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const step = event.key === "ArrowRight" ? RESIZE_KEY_STEP_PX : -RESIZE_KEY_STEP_PX;
      setRailWidth(drawnRailWidth(event.currentTarget as HTMLElement) + step);
      saveRailWidth();
    },
  });
}

function railHead(attrs: ChatRailAttrs): m.Vnode {
  return m("div", { class: "chat-rail-head flex flex-none items-center gap-1 overflow-hidden p-2" }, [
    m(
      "button",
      {
        type: "button",
        class: [
          "chat-rail-new flex flex-none items-center gap-2 rounded-md px-2 py-1.5",
          "text-(length:--font-size-row) text-secondary hover:bg-fill-hover hover:text-primary",
        ].join(" "),
        "aria-label": "New chat",
        onclick: () => attrs.onNew(),
      },
      [plusGlyph(), m("span", "New chat")],
    ),
  ]);
}

/** A button in the phone layout's bars on a touchscreen, the header's and the drawer's, which read as one bar. */
export const BAR_ICON_BUTTON_CLASS =
  "flex size-9 flex-none items-center justify-center rounded-lg text-primary hover:bg-fill-hover";

/** The drawer's header: its name, and "New chat" as a plus at the far end. As tall as the phone header it opens
 *  over, so the two read as one bar. */
function drawerHead(attrs: ChatRailAttrs): m.Vnode {
  return m(
    "div",
    { class: "chat-rail-head flex h-11 flex-none items-center justify-between border-b border-default pr-2 pl-4" },
    [
      m("h1", { class: "type-heading text-primary" }, "Chats"),
      m(
        "button",
        {
          type: "button",
          class: `chat-rail-new ${BAR_ICON_BUTTON_CLASS}`,
          "aria-label": "New chat",
          onclick: () => attrs.onNew(),
        },
        plusGlyph(20),
      ),
    ],
  );
}

function railRow(attrs: ChatRailAttrs, row: ChatRow): m.Vnode {
  const isSelected = row.chatId === attrs.selectedChatId;
  const isDeleting = deletingChatIds.has(row.chatId);
  if (rename !== null && rename.chatId === row.chatId) return renameRow(row, isSelected, attrs.isTouch);
  const status = displayStatus(row);
  return m(
    "button",
    {
      key: row.chatId,
      type: "button",
      class: [
        "chat-rail-row group flex w-full items-center gap-2 rounded-md py-1.5 text-left",
        // A finger's height in the drawer; the rail's rows stay dense under a pointer.
        attrs.isTouch ? "min-h-11" : "",
        isAgentStarted(row) ? "chat-rail-row--nested pr-2 pl-5" : "px-2",
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
      // In the drawer the chat on screen is picked again to close it.
      onclick: () => {
        if (!isSelected || attrs.isInDrawer) attrs.onPick(row.chatId);
      },
      ondblclick: row.isProvisional ? undefined : () => beginRename(row),
      oncontextmenu: (event: MouseEvent) => {
        event.preventDefault();
        if (isDeleting || row.isProvisional) return;
        menuChatId = row.chatId;
        menuReferenceRows = referenceRowsForEvent(attrs, event);
        railMenu.open(anchorForPoint(event.clientX, event.clientY));
      },
    },
    [
      statusDot(row, "flex-none"),
      m("span", { class: "chat-rail-title min-w-0 flex-1 truncate text-(length:--font-size-row)" }, row.title),
      row.isProvisional || (attrs.isTouch && isDeleting) ? null : attrs.isTouch ? rowKebab(row) : renamePencil(row),
    ],
  );
}

/** A control inside the row's button, which buttons cannot nest: a span that acts as one. */
function innerButtonAttrs(label: string, onPress: (element: HTMLElement) => void): m.Attributes {
  return {
    role: "button",
    tabindex: 0,
    "aria-label": label,
    onclick: (event: MouseEvent) => {
      event.stopPropagation();
      onPress(event.currentTarget as HTMLElement);
    },
    onkeydown: (event: KeyboardEvent) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      event.stopPropagation();
      onPress(event.currentTarget as HTMLElement);
    },
  };
}

function renamePencil(row: ChatRow): m.Vnode {
  return m(
    "span",
    {
      // Out of sight it takes no room, the row's gap included, so the title runs to the row's end; it opens up, and
      // the title shortens, only while the row is under the pointer or the pencil has the keyboard. Narrowed to
      // nothing rather than hidden, so a tab still reaches it.
      class:
        "chat-rail-rename -ml-2 w-0 flex-none overflow-hidden rounded p-0 text-faint opacity-0 " +
        "hover:bg-fill-hover hover:text-primary group-hover:ml-0 group-hover:w-auto group-hover:p-0.5 " +
        "group-hover:opacity-100 focus-visible:ml-0 focus-visible:w-auto focus-visible:p-0.5 focus-visible:opacity-100",
      ...innerButtonAttrs("Rename chat", () => beginRename(row)),
    },
    m.trust(icon("edit", { size: 13 })),
  );
}

/** The row's verbs on a phone, where the pencil's hover never happens and nothing right-clicks. */
function rowKebab(row: ChatRow): m.Vnode {
  return m(
    "span",
    {
      class:
        "chat-rail-row-menu -my-1 flex flex-none items-center justify-center rounded-md p-1 text-faint " +
        "hover:bg-fill-hover hover:text-primary",
      "data-chat-row-menu": row.chatId,
      ...innerButtonAttrs("Chat actions", (kebab) => openRowMenu(row, kebab)),
    },
    kebabGlyph(),
  );
}

/** The field a rename is typed into. */
export function renameField(row: ChatRow): m.Vnode {
  const state = rename;
  if (state === null) throw new Error("renameField rendered with no rename in progress");
  return m("input", {
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
  });
}

function renameRow(row: ChatRow, isSelected: boolean, isTouch: boolean): m.Vnode {
  return m(
    "div",
    {
      key: row.chatId,
      class: [
        "chat-rail-row chat-rail-row--renaming flex w-full items-center gap-2 rounded-md py-1.5",
        isTouch ? "min-h-11" : "",
        isAgentStarted(row) ? "pr-2 pl-5" : "px-2",
        isSelected ? "bg-fill-active text-primary" : "text-primary",
      ].join(" "),
      "data-chat-id": row.chatId,
    },
    [statusDot(row, "flex-none"), renameField(row)],
  );
}
