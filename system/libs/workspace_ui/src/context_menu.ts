/**
 * The element menu's one installer (docs/system/blueprint/element-reference-menu/, section
 * 4.1): a ``contextmenu`` listener on the document that yields to a page's own handling (an
 * event already ``defaultPrevented``), captures the target and the selection as they were at
 * the click, builds the rows, and opens the menu at the pointer. A right-click with Shift held
 * is left to the browser's own menu. Where the Imbue Studio desktop app draws the menu natively
 * (section 11), the installer opens nothing: it keeps the rows for the app, which reads them and
 * runs the one picked through the page's ``__studioContextMenu`` bridge.
 *
 * Rendering is the one part a page may replace: a Mithril page hands ``open`` an opener backed
 * by the shared Menu (``components/contextMenuOpener.ts``), and a page without a framework --
 * an app an agent built -- takes the renderer here, a fixed card of buttons with inline styles.
 * Built as its own library entry into the shell's static output and served by every app at
 * ``/_static/context_menu.js`` from its own origin, beside ``app_contract.js``; so it imports
 * the reference and row modules alone, and touches no message primitive (the draft goes
 * through the connection the page already holds, and an opened link is a click on it, which
 * the app contract follows).
 */

import { scopeOfHandshake, type ReferenceHandshake, type ReferenceScope } from "./element_reference";
import {
  elementMenuRows,
  hostMenuSections,
  targetOfEvent,
  type ContextMenuActionRow,
  type ContextMenuCheckRow,
  type ContextMenuRow,
  type ContextMenuSubmenuChildRow,
  type ContextMenuTarget,
  type HostMenuSections,
  type OpenLink,
} from "./context_menu_rows";

/** The connection the page holds to the shell: what the installer drafts through. */
export interface ContextMenuConnection {
  hasShell(): boolean;
  draftText(text: string): void;
}

/** Where the menu opens: the pointer's viewport position. */
export interface ContextMenuPoint {
  x: number;
  y: number;
}

/** The handshake fields the installer reads: the scope, and whether the desktop app draws the menu. */
export interface ContextMenuHandshake extends ReferenceHandshake {
  drawsContextMenus?: boolean;
}

interface ContextMenuBaseOptions {
  /** The page's last handshake, read at every right-click (so a getter); the scope of every reference. */
  handshake?: () => ContextMenuHandshake | null;
  /** Whether the Imbue Studio desktop app draws the menu natively; the handshake's ``drawsContextMenus`` when unset. */
  isDrawnByHost?: () => boolean;
  /** The scope of a reference, for a page that knows more than its handshake says (the shell, which is no
   *  frame's page); ``handshake`` when unset. */
  scope?: (target: ContextMenuTarget) => ReferenceScope;
  /** A page's own rows for the target, first in the menu. */
  extraRows?: (target: ContextMenuTarget) => readonly ContextMenuRow[];
  /** Draw the rows at the point, for the right-click on ``target``; the framework-free renderer when unset. */
  open?: (rows: readonly ContextMenuRow[], point: ContextMenuPoint, target: ContextMenuTarget) => void;
  /** The document to listen on; the page's own when unset (a test hands in another). */
  document?: Document;
}

/** A page a shell may frame: a draft goes through its connection unless it says otherwise. */
interface ConnectedContextMenuOptions extends ContextMenuBaseOptions {
  connection: ContextMenuConnection;
  /** Where a draft goes; the connection's ``draftText`` when unset (section 3.4). */
  draft?: (text: string) => void;
  /** Whether Explain and Modify can run; ``connection.hasShell()`` when unset (section 4.7). */
  isDraftAvailable?: () => boolean;
}

/** A page with a draft route of its own and no connection (the shell, whose route is its store). */
interface RoutedContextMenuOptions extends ContextMenuBaseOptions {
  connection?: undefined;
  draft: (text: string) => void;
  isDraftAvailable: () => boolean;
  /** Where "Open link" goes. */
  openLink: OpenLink;
}

export type ContextMenuOptions = ConnectedContextMenuOptions | RoutedContextMenuOptions;

/** Where a draft goes and whether it can, and where "Open link" goes, from either shape of the options. */
function routesOf(options: ContextMenuOptions): {
  draft: (text: string) => void;
  isDraftAvailable: () => boolean;
  openLink: OpenLink;
} {
  if (options.connection === undefined) {
    return { draft: options.draft, isDraftAvailable: options.isDraftAvailable, openLink: options.openLink };
  }
  const { connection } = options;
  return {
    draft: options.draft ?? ((text: string) => connection.draftText(text)),
    isDraftAvailable: options.isDraftAvailable ?? (() => connection.hasShell()),
    openLink: (anchor) => anchor.click(),
  };
}

/** The global the desktop app reads a right-click's rows through and runs a picked one by; the vendored
 *  ``page-menu-bridge.json`` pins it, with the bounds the app keeps a row within (context_menu.contract.test.ts). */
export const HOST_MENU_GLOBAL = "__studioContextMenu";

/** A row of a submenu as the desktop app receives it: its data, without its action. */
export type HostMenuLeafItem =
  | { kind: "action"; key: string; label: string; isDisabled: boolean; tooltip: string | null; symbol: string | null }
  | { kind: "check"; key: string; label: string; isChecked: boolean; tooltip: string | null; symbol: string | null }
  | { kind: "divider" };

/** A row as the desktop app receives it; a submenu carries its rows, read when the right-click happened. */
export type HostMenuItem =
  | HostMenuLeafItem
  | { kind: "submenu"; label: string; tooltip: string | null; symbol: string | null; items: HostMenuLeafItem[] };

export interface HostMenuItemSections {
  page: HostMenuItem[];
  link: HostMenuItem[];
  reference: HostMenuItem[];
}

/** What the desktop app calls on the page, through ``HOST_MENU_GLOBAL``. */
export interface HostMenuBridge {
  /** The last right-click's rows, or null when it was left to the browser's menu. */
  sections(): HostMenuItemSections | null;
  /** Run the last right-click's row of this key, a submenu's included, once the page has focus. */
  select(key: string): Promise<void>;
}

/** How long a row the desktop app runs waits for the page to take focus before it runs anyway. */
const HOST_SELECT_FOCUS_WAIT_MS = 500;

type PickableRow = ContextMenuActionRow | ContextMenuCheckRow;

/** A right-click's rows as the desktop app reads them, and the row each key picks: each submenu's rows are read
 *  once, here, so what the app shows and what a pick runs are the same rows. */
interface HostMenuSnapshot {
  sections: HostMenuItemSections;
  rowsByKey: Map<string, PickableRow>;
}

function hostMenuLeafItemOf(row: ContextMenuSubmenuChildRow, rowsByKey: Map<string, PickableRow>): HostMenuLeafItem {
  if (row.kind === "divider") return { kind: "divider" };
  if (!rowsByKey.has(row.key)) rowsByKey.set(row.key, row);
  const shared = { key: row.key, label: row.label, tooltip: row.tooltip ?? null, symbol: row.symbol ?? null };
  if (row.kind === "check") return { kind: "check", ...shared, isChecked: row.isChecked };
  return { kind: "action", ...shared, isDisabled: row.isDisabled === true };
}

function hostMenuItemOf(row: ContextMenuRow, rowsByKey: Map<string, PickableRow>): HostMenuItem {
  if (row.kind !== "submenu") return hostMenuLeafItemOf(row, rowsByKey);
  return {
    kind: "submenu",
    label: row.label,
    tooltip: row.tooltip ?? null,
    symbol: row.symbol ?? null,
    items: row.rows().map((child) => hostMenuLeafItemOf(child, rowsByKey)),
  };
}

function hostMenuSnapshotOf(sections: HostMenuSections): HostMenuSnapshot {
  const rowsByKey = new Map<string, PickableRow>();
  const itemsOf = (rows: readonly ContextMenuRow[]): HostMenuItem[] =>
    rows.map((row) => hostMenuItemOf(row, rowsByKey));
  return {
    sections: { page: itemsOf(sections.page), link: itemsOf(sections.link), reference: itemsOf(sections.reference) },
    rowsByKey,
  };
}

/** Resolves once the page has focus, asking for it, or after the wait. A row the desktop app runs arrives while
 *  the window or the frame may be unfocused (a right-click focuses neither), and the clipboard refuses a page
 *  without focus. */
function whenFocused(ownerDocument: Document): Promise<void> {
  const view = ownerDocument.defaultView;
  if (view === null || ownerDocument.hasFocus()) return Promise.resolve();
  return new Promise((resolve) => {
    const done = (): void => {
      view.clearTimeout(timer);
      view.removeEventListener("focus", done);
      resolve();
    };
    const timer = view.setTimeout(done, HOST_SELECT_FOCUS_WAIT_MS);
    view.addEventListener("focus", done);
    view.focus();
  });
}

function hostMenuBridgeOf(currentSnapshot: () => HostMenuSnapshot | null, ownerDocument: Document): HostMenuBridge {
  return {
    sections: () => currentSnapshot()?.sections ?? null,
    select: async (key) => {
      const row = currentSnapshot()?.rowsByKey.get(key);
      if (row === undefined || (row.kind === "action" && row.isDisabled === true)) return;
      await whenFocused(ownerDocument);
      if (row.kind === "check") row.onToggle();
      else row.onSelect();
    },
  };
}

/** Marks the document the installer listens on, so a second install is a no-op. */
export const CONTEXT_MENU_INSTALLED_ATTR = "data-element-context-menu";
/** Marks the framework-free renderer's card and rows, so a test can find them. */
export const CONTEXT_MENU_CARD_ATTR = "data-context-menu";
export const CONTEXT_MENU_SUBMENU_ATTR = "data-context-menu-submenu";
export const CONTEXT_MENU_ROW_ATTR = "data-context-menu-row";

const uninstallerByDocument = new WeakMap<Document, () => void>();

/**
 * Install the element menu on the page. Answers the uninstaller; a document already carrying
 * the menu answers the uninstaller of the install it has, or a no-op when that install came
 * from another copy of this module (one bundled from source beside the served file), which
 * left the mark but is not this copy's to undo.
 */
export function installElementContextMenu(options: ContextMenuOptions): () => void {
  const ownerDocument = options.document ?? document;
  const existing = uninstallerByDocument.get(ownerDocument);
  if (existing !== undefined) return existing;
  if (ownerDocument.documentElement.hasAttribute(CONTEXT_MENU_INSTALLED_ATTR)) return () => undefined;
  // A page's own renderer is its owner's to close; the default one is closed here on uninstall.
  const renderer: ContextMenuRenderer =
    options.open === undefined ? createDefaultRenderer(ownerDocument) : { open: options.open };
  const { draft, isDraftAvailable, openLink } = routesOf(options);
  const scopeOf = options.scope ?? (() => scopeOfHandshake(options.handshake?.() ?? null));
  const isDrawnByHost = options.isDrawnByHost ?? (() => options.handshake?.()?.drawsContextMenus === true);
  let hostSnapshot: HostMenuSnapshot | null = null;

  // Cleared in the capture phase, before any handler of the page can stop the event short of the document: a
  // right-click that never reaches onContextMenu would otherwise leave the previous right-click's rows on the bridge.
  const forgetHostSections = (): void => {
    hostSnapshot = null;
  };
  const onContextMenu = (event: MouseEvent): void => {
    if (event.defaultPrevented || event.shiftKey) return;
    const target = targetOfEvent(event, ownerDocument);
    if (isDrawnByHost()) {
      hostSnapshot = hostMenuSnapshotOf(
        hostMenuSections(
          target,
          scopeOf(target),
          draft,
          isDraftAvailable(),
          options.extraRows?.(target) ?? [],
          openLink,
        ),
      );
      return;
    }
    event.preventDefault();
    const rows = elementMenuRows(
      target,
      scopeOf(target),
      draft,
      isDraftAvailable(),
      options.extraRows?.(target) ?? [],
      openLink,
    );
    renderer.open(rows, { x: event.clientX, y: event.clientY }, target);
  };

  ownerDocument.addEventListener("contextmenu", forgetHostSections, true);
  // own-context-menu: element-menu -- the element menu itself, which draws its HTML menu only where the desktop app draws none
  ownerDocument.addEventListener("contextmenu", onContextMenu);
  ownerDocument.documentElement.setAttribute(CONTEXT_MENU_INSTALLED_ATTR, "");
  const view = ownerDocument.defaultView;
  if (view !== null)
    Reflect.set(
      view,
      HOST_MENU_GLOBAL,
      hostMenuBridgeOf(() => hostSnapshot, ownerDocument),
    );
  const uninstall = (): void => {
    renderer.close?.();
    if (view !== null) Reflect.deleteProperty(view, HOST_MENU_GLOBAL);
    ownerDocument.removeEventListener("contextmenu", forgetHostSections, true);
    ownerDocument.removeEventListener("contextmenu", onContextMenu);
    ownerDocument.documentElement.removeAttribute(CONTEXT_MENU_INSTALLED_ATTR);
    uninstallerByDocument.delete(ownerDocument);
  };
  uninstallerByDocument.set(ownerDocument, uninstall);
  return uninstall;
}

// The framework-free renderer

const CARD_STYLE = [
  "position: fixed",
  "z-index: 2147483000",
  "min-width: 180px",
  "max-width: 320px",
  "padding: 4px 0",
  "border: 1px solid rgba(0, 0, 0, 0.15)",
  "border-radius: 8px",
  "background: #ffffff",
  "color: #1a1a1a",
  "box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18)",
  "font: 13px/1.4 ui-sans-serif, system-ui, sans-serif",
].join("; ");
const ROW_STYLE = [
  "display: flex",
  "align-items: center",
  "gap: 6px",
  "width: 100%",
  "padding: 6px 12px",
  "border: 0",
  "background: transparent",
  "color: inherit",
  "font: inherit",
  "text-align: left",
  "cursor: pointer",
].join("; ");
const DISABLED_ROW_STYLE = `${ROW_STYLE}; color: #9a9a9a; cursor: default`;
const ROW_COLOR_BY_TONE: Readonly<Partial<Record<NonNullable<ContextMenuActionRow["tone"]>, string>>> = {
  danger: "#c62828",
  quiet: "#6b6b6b",
};
const DIVIDER_STYLE = "margin: 4px 0; border: 0; border-top: 1px solid rgba(0, 0, 0, 0.1)";
const HOVER_BACKGROUND = "rgba(0, 0, 0, 0.06)";
/** Gap kept between the card and each viewport edge. */
const EDGE_MARGIN = 6;
/** How long the pointer rests on a submenu row before its submenu opens: a sweep across rows opens none. */
const SUBMENU_OPEN_DELAY_MS = 120;
/** How long an open submenu outlives the pointer moving onto another row of the menu, so a diagonal trip from the
 *  row to the submenu that grazes a neighbour does not close it. */
const SUBMENU_CLOSE_GRACE_MS = 300;
/** How far a submenu tucks under the menu's edge, and rises so its first row lines up with the row opening it. */
const SUBMENU_OVERLAP = 4;

/** What draws the menu: ``open`` puts the rows at the point; ``close``, when the renderer has one, takes an open
 *  menu down with the listeners it holds. */
interface ContextMenuRenderer {
  open: (rows: readonly ContextMenuRow[], point: ContextMenuPoint, target: ContextMenuTarget) => void;
  close?: () => void;
}

/** Keep a card of this size inside the viewport, sliding it left or up from where it would start. */
function clampedOrigin(view: Window | null, x: number, y: number, width: number, height: number): ContextMenuPoint {
  const viewportWidth = view?.innerWidth ?? width;
  const viewportHeight = view?.innerHeight ?? height;
  return {
    x: Math.max(EDGE_MARGIN, Math.min(x, viewportWidth - width - EDGE_MARGIN)),
    y: Math.max(EDGE_MARGIN, Math.min(y, viewportHeight - height - EDGE_MARGIN)),
  };
}

/** A renderer that draws the rows as a fixed card, a submenu as a second card beside its row, and closes them on a
 *  press outside, Escape, or a pick. */
export function createDefaultRenderer(ownerDocument: Document): Required<ContextMenuRenderer> {
  const view = ownerDocument.defaultView;
  let card: HTMLElement | null = null;
  let submenu: { card: HTMLElement; row: HTMLElement } | null = null;
  let openTimer: number | null = null;
  let closeTimer: number | null = null;

  const clearTimer = (timer: number | null): null => {
    if (timer !== null) view?.clearTimeout(timer);
    return null;
  };
  const closeSubmenu = (): void => {
    openTimer = clearTimer(openTimer);
    closeTimer = clearTimer(closeTimer);
    if (submenu === null) return;
    submenu.card.remove();
    submenu.row.style.background = "transparent";
    submenu = null;
  };
  const close = (): void => {
    closeSubmenu();
    if (card === null) return;
    card.remove();
    card = null;
    ownerDocument.removeEventListener("mousedown", onPressOutside, true);
    ownerDocument.removeEventListener("keydown", onKeydown, true);
  };
  const isInsideMenu = (node: Node): boolean =>
    (card !== null && card.contains(node)) || (submenu !== null && submenu.card.contains(node));
  const onPressOutside = (event: MouseEvent): void => {
    if (event.target instanceof Node && isInsideMenu(event.target)) return;
    close();
  };
  const onKeydown = (event: KeyboardEvent): void => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    close();
  };

  /** A new card holding ``rows``, not yet in the document. */
  const cardOf = (rows: readonly ContextMenuRow[], isSubmenu: boolean): HTMLElement => {
    const next = ownerDocument.createElement("div");
    next.setAttribute(isSubmenu ? CONTEXT_MENU_SUBMENU_ATTR : CONTEXT_MENU_CARD_ATTR, "");
    next.setAttribute("role", "menu");
    next.setAttribute("style", CARD_STYLE);
    // A right-click on the card itself is handled here, so the installer yields to it (as it
    // yields to the shared Menu's sheet) rather than describing one of the card's own buttons.
    // own-context-menu: dismisses -- a right-click on the open menu's own card describes nothing
    next.addEventListener("contextmenu", (event) => event.preventDefault());
    // A card with a check row keeps the check's column on every row, so the labels line up.
    const hasCheckColumn = rows.some((row) => row.kind === "check");
    for (const row of rows) next.appendChild(rowElementOf(row, isSubmenu, hasCheckColumn));
    return next;
  };

  const rowElementOf = (row: ContextMenuRow, isInSubmenu: boolean, hasCheckColumn: boolean): HTMLElement => {
    if (row.kind === "divider") {
      const divider = ownerDocument.createElement("hr");
      divider.setAttribute("style", DIVIDER_STYLE);
      return divider;
    }
    const button = ownerDocument.createElement("button");
    button.type = "button";
    button.setAttribute("role", row.kind === "check" ? "menuitemcheckbox" : "menuitem");
    button.setAttribute(CONTEXT_MENU_ROW_ATTR, row.key);
    const isDisabled = row.kind === "action" && row.isDisabled === true;
    button.setAttribute("style", isDisabled ? DISABLED_ROW_STYLE : ROW_STYLE);
    if (isDisabled) button.setAttribute("aria-disabled", "true");
    const toneColor = row.kind === "action" && row.tone !== undefined ? ROW_COLOR_BY_TONE[row.tone] : undefined;
    if (!isDisabled && toneColor !== undefined) button.style.color = toneColor;
    if (row.tooltip !== undefined) button.title = row.tooltip;
    if (row.kind === "check") button.setAttribute("aria-checked", row.isChecked ? "true" : "false");
    if (hasCheckColumn) {
      const mark = ownerDocument.createElement("span");
      mark.setAttribute("style", "display: inline-block; width: 1em; text-align: center");
      mark.textContent = row.kind === "check" && row.isChecked ? "✓" : "";
      button.appendChild(mark);
    }
    const label = ownerDocument.createElement("span");
    label.setAttribute("style", "flex: 1");
    label.textContent = row.label;
    button.appendChild(label);
    // The press must not move focus off the target the edit rows act on, nor collapse the
    // selection there, as the shared Menu keeps its sheet's press from doing; the click still fires.
    button.addEventListener("mousedown", (event) => event.preventDefault());

    if (row.kind === "submenu") {
      button.setAttribute("aria-haspopup", "menu");
      const chevron = ownerDocument.createElement("span");
      chevron.setAttribute("aria-hidden", "true");
      chevron.textContent = "›";
      button.appendChild(chevron);
      button.addEventListener("mouseenter", () => {
        button.style.background = HOVER_BACKGROUND;
        closeTimer = clearTimer(closeTimer);
        if (submenu?.row === button) return;
        openTimer = clearTimer(openTimer);
        openTimer = view?.setTimeout(() => openSubmenu(button, row.rows()), SUBMENU_OPEN_DELAY_MS) ?? null;
      });
      button.addEventListener("mouseleave", () => {
        openTimer = clearTimer(openTimer);
        if (submenu?.row !== button) button.style.background = "transparent";
      });
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        if (submenu?.row !== button) openSubmenu(button, row.rows());
      });
      return button;
    }

    button.addEventListener("mouseenter", () => {
      if (!isDisabled) button.style.background = HOVER_BACKGROUND;
      // Another row of the menu: the open submenu goes once the grace runs out, unless the pointer reaches it.
      if (!isInSubmenu && submenu !== null && closeTimer === null) {
        closeTimer = view?.setTimeout(closeSubmenu, SUBMENU_CLOSE_GRACE_MS) ?? null;
      }
    });
    button.addEventListener("mouseleave", () => {
      button.style.background = "transparent";
    });
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      if (isDisabled) return;
      close();
      if (row.kind === "check") row.onToggle();
      else row.onSelect();
    });
    return button;
  };

  /** Open ``rows`` as a submenu beside ``row``: after the menu's edge, or before it where that would leave the
   *  viewport, its first row level with ``row``. */
  const openSubmenu = (row: HTMLElement, rows: readonly ContextMenuSubmenuChildRow[]): void => {
    closeSubmenu();
    if (card === null) return;
    const next = cardOf(rows, true);
    next.addEventListener("mouseenter", () => {
      closeTimer = clearTimer(closeTimer);
    });
    ownerDocument.body.appendChild(next);
    const menuBox = card.getBoundingClientRect();
    const rowBox = row.getBoundingClientRect();
    const width = next.offsetWidth;
    const height = next.offsetHeight;
    const viewportWidth = view?.innerWidth ?? menuBox.right + width;
    const after = menuBox.right - SUBMENU_OVERLAP;
    const x = after + width + EDGE_MARGIN <= viewportWidth ? after : menuBox.left - width + SUBMENU_OVERLAP;
    const origin = clampedOrigin(view, x, rowBox.top - SUBMENU_OVERLAP, width, height);
    next.style.left = `${origin.x}px`;
    next.style.top = `${origin.y}px`;
    row.style.background = HOVER_BACKGROUND;
    submenu = { card: next, row };
  };

  const open = (rows: readonly ContextMenuRow[], point: ContextMenuPoint): void => {
    close();
    const next = cardOf(rows, false);
    ownerDocument.body.appendChild(next);
    // Placed after it is in the document, so its size is known.
    const origin = clampedOrigin(view, point.x, point.y, next.offsetWidth, next.offsetHeight);
    next.style.left = `${origin.x}px`;
    next.style.top = `${origin.y}px`;
    card = next;
    ownerDocument.addEventListener("mousedown", onPressOutside, true);
    ownerDocument.addEventListener("keydown", onKeydown, true);
  };
  return { open, close };
}
