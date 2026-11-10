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
  type ContextMenuRow,
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
  /** Draw the rows at the point; the framework-free renderer when unset. */
  open?: (rows: readonly ContextMenuRow[], point: ContextMenuPoint) => void;
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

/** The global the desktop app reads a right-click's rows through and runs a picked one by (the mngr repo's
 *  ``apps/minds/electron/context-menu.js`` names it too). */
export const HOST_MENU_GLOBAL = "__studioContextMenu";

/** A row as the desktop app receives it: its data, without its action. */
export type HostMenuItem =
  | { kind: "action"; key: string; label: string; isDisabled: boolean; tooltip: string | null; symbol: string | null }
  | { kind: "divider" };

export interface HostMenuItemSections {
  page: HostMenuItem[];
  link: HostMenuItem[];
  reference: HostMenuItem[];
}

/** What the desktop app calls on the page, through ``HOST_MENU_GLOBAL``. */
export interface HostMenuBridge {
  /** The last right-click's rows, or null when it was left to the browser's menu. */
  sections(): HostMenuItemSections | null;
  /** Run the last right-click's row of this key, once the page has focus. */
  select(key: string): Promise<void>;
}

/** How long a row the desktop app runs waits for the page to take focus before it runs anyway. */
const HOST_SELECT_FOCUS_WAIT_MS = 500;

function hostMenuItemOf(row: ContextMenuRow): HostMenuItem {
  if (row.kind === "divider") return { kind: "divider" };
  return {
    kind: "action",
    key: row.key,
    label: row.label,
    isDisabled: row.isDisabled === true,
    tooltip: row.tooltip ?? null,
    symbol: row.symbol ?? null,
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

function hostMenuBridgeOf(currentSections: () => HostMenuSections | null, ownerDocument: Document): HostMenuBridge {
  const rowsOf = (sections: HostMenuSections): readonly ContextMenuRow[] => [
    ...sections.page,
    ...sections.link,
    ...sections.reference,
  ];
  return {
    sections: () => {
      const sections = currentSections();
      if (sections === null) return null;
      return {
        page: sections.page.map(hostMenuItemOf),
        link: sections.link.map(hostMenuItemOf),
        reference: sections.reference.map(hostMenuItemOf),
      };
    },
    select: async (key) => {
      const sections = currentSections();
      if (sections === null) return;
      const row = rowsOf(sections).find(
        (candidate): candidate is ContextMenuActionRow => candidate.kind === "action" && candidate.key === key,
      );
      if (row === undefined || row.isDisabled === true) return;
      await whenFocused(ownerDocument);
      row.onSelect();
    },
  };
}

/** Marks the document the installer listens on, so a second install is a no-op. */
export const CONTEXT_MENU_INSTALLED_ATTR = "data-element-context-menu";
/** Marks the framework-free renderer's card and rows, so a test can find them. */
export const CONTEXT_MENU_CARD_ATTR = "data-context-menu";
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
  let hostSections: HostMenuSections | null = null;

  const onContextMenu = (event: MouseEvent): void => {
    hostSections = null;
    if (event.defaultPrevented || event.shiftKey) return;
    const target = targetOfEvent(event, ownerDocument);
    if (isDrawnByHost()) {
      hostSections = hostMenuSections(
        target,
        scopeOf(target),
        draft,
        isDraftAvailable(),
        options.extraRows?.(target) ?? [],
        openLink,
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
    renderer.open(rows, { x: event.clientX, y: event.clientY });
  };

  ownerDocument.addEventListener("contextmenu", onContextMenu);
  ownerDocument.documentElement.setAttribute(CONTEXT_MENU_INSTALLED_ATTR, "");
  const view = ownerDocument.defaultView;
  if (view !== null)
    Reflect.set(
      view,
      HOST_MENU_GLOBAL,
      hostMenuBridgeOf(() => hostSections, ownerDocument),
    );
  const uninstall = (): void => {
    renderer.close?.();
    if (view !== null) Reflect.deleteProperty(view, HOST_MENU_GLOBAL);
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
  "display: block",
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
const DIVIDER_STYLE = "margin: 4px 0; border: 0; border-top: 1px solid rgba(0, 0, 0, 0.1)";
/** Gap kept between the card and each viewport edge. */
const EDGE_MARGIN = 6;

/** What draws the menu: ``open`` puts the rows at the point; ``close``, when the renderer has one, takes an open
 *  menu down with the listeners it holds. */
interface ContextMenuRenderer {
  open: (rows: readonly ContextMenuRow[], point: ContextMenuPoint) => void;
  close?: () => void;
}

/** A renderer that draws the rows as a fixed card and closes it on a press outside, Escape, or a pick. */
export function createDefaultRenderer(ownerDocument: Document): Required<ContextMenuRenderer> {
  let card: HTMLElement | null = null;

  const close = (): void => {
    if (card === null) return;
    card.remove();
    card = null;
    ownerDocument.removeEventListener("mousedown", onPressOutside, true);
    ownerDocument.removeEventListener("keydown", onKeydown, true);
  };
  const onPressOutside = (event: MouseEvent): void => {
    if (card !== null && event.target instanceof Node && card.contains(event.target)) return;
    close();
  };
  const onKeydown = (event: KeyboardEvent): void => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    close();
  };

  const open = (rows: readonly ContextMenuRow[], point: ContextMenuPoint): void => {
    close();
    const view = ownerDocument.defaultView;
    const next = ownerDocument.createElement("div");
    next.setAttribute(CONTEXT_MENU_CARD_ATTR, "");
    next.setAttribute("role", "menu");
    next.setAttribute("style", CARD_STYLE);
    // A right-click on the card itself is handled here, so the installer yields to it (as it
    // yields to the shared Menu's sheet) rather than describing one of the card's own buttons.
    next.addEventListener("contextmenu", (event) => event.preventDefault());
    for (const row of rows) {
      if (row.kind === "divider") {
        const divider = ownerDocument.createElement("hr");
        divider.setAttribute("style", DIVIDER_STYLE);
        next.appendChild(divider);
        continue;
      }
      const button = ownerDocument.createElement("button");
      button.type = "button";
      button.setAttribute("role", "menuitem");
      button.setAttribute(CONTEXT_MENU_ROW_ATTR, row.key);
      button.textContent = row.label;
      const isDisabled = row.isDisabled === true;
      button.setAttribute("style", isDisabled ? DISABLED_ROW_STYLE : ROW_STYLE);
      if (isDisabled) button.setAttribute("aria-disabled", "true");
      if (row.tooltip !== undefined) button.title = row.tooltip;
      button.addEventListener("mouseenter", () => {
        if (!isDisabled) button.style.background = "rgba(0, 0, 0, 0.06)";
      });
      button.addEventListener("mouseleave", () => {
        button.style.background = "transparent";
      });
      // The press must not move focus off the target the edit rows act on, nor collapse the
      // selection there, as the shared Menu keeps its sheet's press from doing; the click still fires.
      button.addEventListener("mousedown", (event) => event.preventDefault());
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        if (isDisabled) return;
        close();
        row.onSelect();
      });
      next.appendChild(button);
    }
    ownerDocument.body.appendChild(next);
    // Placed after it is in the document, so its size is known: kept inside the viewport by
    // sliding it left or up when it would overflow.
    const width = next.offsetWidth;
    const height = next.offsetHeight;
    const viewportWidth = view?.innerWidth ?? width;
    const viewportHeight = view?.innerHeight ?? height;
    const left = Math.max(EDGE_MARGIN, Math.min(point.x, viewportWidth - width - EDGE_MARGIN));
    const top = Math.max(EDGE_MARGIN, Math.min(point.y, viewportHeight - height - EDGE_MARGIN));
    next.style.left = `${left}px`;
    next.style.top = `${top}px`;
    card = next;
    ownerDocument.addEventListener("mousedown", onPressOutside, true);
    ownerDocument.addEventListener("keydown", onKeydown, true);
  };
  return { open, close };
}
