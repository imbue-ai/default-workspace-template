/**
 * The element menu's one installer (docs/system/blueprint/element-reference-menu/, section
 * 4.1): a ``contextmenu`` listener on the document that yields to a page's own handling (an
 * event already ``defaultPrevented``), captures the target and the selection as they were at
 * the click, builds the rows, and opens the menu at the pointer. Where the Imbue Studio desktop
 * app draws a native menu (section 11), a right-click on text being edited, on a selection the
 * user made, or on a plain link, and any with Shift held, is left to it.
 *
 * Rendering is the one part a page may replace: a Mithril page hands ``open`` an opener backed
 * by the shared Menu (``components/contextMenuOpener.ts``), and a page without a framework --
 * an app an agent built -- takes the renderer here, which draws the same rows as cards of
 * buttons wearing the workspace theme's colours.
 * Built as its own library entry into the shell's static output and served by every app at
 * ``/_static/context_menu.js`` from its own origin, beside ``app_contract.js``; so it imports
 * the reference and row modules and the icon strings alone, and touches no message primitive (the draft goes
 * through the connection the page already holds, and an opened link is a click on it, which
 * the app contract follows).
 */

import { icon } from "./components/icons";
import { scopeOfHandshake, type ReferenceHandshake, type ReferenceScope } from "./element_reference";
import {
  elementMenuRows,
  isNativeMenuTarget,
  isPointOnSelection,
  joinRowGroups,
  targetOfEvent,
  type ContextMenuActionRow,
  type ContextMenuCheckRow,
  type ContextMenuRow,
  type ContextMenuSubmenuChildRow,
  type ContextMenuSubmenuRow,
  type ContextMenuTarget,
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

/** The handshake fields the installer reads: the scope, and whether the desktop app draws a native menu. */
export interface ContextMenuHandshake extends ReferenceHandshake {
  drawsContextMenus?: boolean;
}

export type ContextMenuOpen = (
  rows: readonly ContextMenuRow[],
  point: ContextMenuPoint,
  target: ContextMenuTarget,
  followingRows: readonly ContextMenuRow[],
) => void;

interface ContextMenuBaseOptions {
  /** The page's last handshake, read at every right-click (so a getter); the scope of every reference. */
  handshake?: () => ContextMenuHandshake | null;
  /** Whether the Imbue Studio desktop app draws a native menu for a right-click the page leaves alone; the
   *  handshake's ``drawsContextMenus`` when unset (the shell, which is no frame's page, has it from its store). */
  hasNativeMenu?: () => boolean;
  /** The scope of a reference, for a page that knows more than its handshake says (the shell, which is no
   *  frame's page); ``handshake`` when unset. */
  scope?: (target: ContextMenuTarget) => ReferenceScope;
  /** A page's own rows for the target, first in the menu. */
  extraRows?: (target: ContextMenuTarget) => readonly ContextMenuRow[];
  /** Draw the rows at the point, for the right-click on ``target``; the framework-free renderer when unset.
   *  ``followingRows`` is ``rows`` without the page's own: what a page that draws its own rows itself, reading them
   *  afresh while the menu is up, puts after them. */
  open?: ContextMenuOpen;
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

/** Marks the document the installer listens on, so a second install is a no-op. */
export const CONTEXT_MENU_INSTALLED_ATTR = "data-element-context-menu";
/** Marks the framework-free renderer's card and rows, so a test can find them. */
export const CONTEXT_MENU_CARD_ATTR = "data-context-menu";
export const CONTEXT_MENU_SUBMENU_ATTR = "data-context-menu-submenu";
export const CONTEXT_MENU_ROW_ATTR = "data-context-menu-row";

/** A press that opens a menu, and whether it landed on the selection as the user had left it. */
interface MenuPress {
  x: number;
  y: number;
  wasOnSelection: boolean;
}

/** How far a ``contextmenu`` event may sit from the press it came from: a pointer event's coordinates may be
 *  fractional where that event's are whole. */
const PRESS_SLOP_PX = 1;

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
  const hasNativeMenu = options.hasNativeMenu ?? (() => options.handshake?.()?.drawsContextMenus === true);

  // macOS selects the word or the link under a right-click before ``contextmenu`` fires, so the selection read
  // then lies under the pointer whether or not the user made it. The press comes first, and is asked instead.
  let press: MenuPress | null = null;
  const onPointerDown = (event: MouseEvent): void => {
    // The secondary button, or the primary one with Control, which is macOS's other right-click.
    const opensMenu = event.button === 2 || (event.button === 0 && event.ctrlKey);
    if (!opensMenu || !hasNativeMenu()) {
      press = null;
      return;
    }
    const { clientX, clientY } = event;
    press = { x: clientX, y: clientY, wasOnSelection: isPointOnSelection(ownerDocument, clientX, clientY) };
  };
  /** Whether the right-click landed on a selection the user had made: what its press found, or the selection as
   *  it stands for a menu no press here opened (the keyboard's). */
  const isOnSelection = (event: MouseEvent, pressed: MenuPress | null): boolean => {
    const isFromPress =
      pressed !== null &&
      Math.abs(pressed.x - event.clientX) <= PRESS_SLOP_PX &&
      Math.abs(pressed.y - event.clientY) <= PRESS_SLOP_PX;
    return isFromPress ? pressed.wasOnSelection : isPointOnSelection(ownerDocument, event.clientX, event.clientY);
  };

  const onContextMenu = (event: MouseEvent): void => {
    const pressed = press;
    press = null;
    if (event.defaultPrevented) return;
    const target = targetOfEvent(event, ownerDocument);
    const ownRows = options.extraRows?.(target) ?? [];
    if (
      hasNativeMenu() &&
      (event.shiftKey || isNativeMenuTarget(target, ownRows.length > 0, isOnSelection(event, pressed)))
    ) {
      return;
    }
    event.preventDefault();
    const followingRows = elementMenuRows(target, scopeOf(target), draft, isDraftAvailable(), [], openLink);
    const rows = joinRowGroups([ownRows, followingRows]);
    renderer.open(rows, { x: event.clientX, y: event.clientY }, target, followingRows);
  };

  // own-context-menu: element-menu -- the element menu itself, the one menu every other right-click's rows join
  ownerDocument.addEventListener("contextmenu", onContextMenu);
  ownerDocument.addEventListener("pointerdown", onPointerDown, true);
  ownerDocument.documentElement.setAttribute(CONTEXT_MENU_INSTALLED_ATTR, "");
  const uninstall = (): void => {
    renderer.close?.();
    ownerDocument.removeEventListener("contextmenu", onContextMenu);
    ownerDocument.removeEventListener("pointerdown", onPointerDown, true);
    ownerDocument.documentElement.removeAttribute(CONTEXT_MENU_INSTALLED_ATTR);
    uninstallerByDocument.delete(ownerDocument);
  };
  uninstallerByDocument.set(ownerDocument, uninstall);
  return uninstall;
}

// The framework-free renderer

// The colours, radius and font are the workspace theme's (the page kit's ``workspace_theme.css`` sets them), so the
// menu looks like the shared Menu a built-in app draws; each falls back to a fixed value on a page without the kit.
const CARD_STYLE = [
  "position: fixed",
  "z-index: 2147483000",
  "min-width: 180px",
  "max-width: 320px",
  "padding: 4px 0",
  "border: 1px solid var(--c-border, rgba(0, 0, 0, 0.15))",
  "border-radius: var(--radius-md, 8px)",
  "background: var(--c-surface, #ffffff)",
  "color: var(--c-text-primary, #1a1a1a)",
  "box-shadow: var(--c-shadow-overlay, 0 8px 24px rgba(0, 0, 0, 0.18))",
  "font: var(--font-size-row, 13px)/1.4 var(--font-sans, ui-sans-serif, system-ui, sans-serif)",
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
const DISABLED_ROW_STYLE = `${ROW_STYLE}; color: var(--c-text-faint, #9a9a9a); cursor: default`;
/** The colour of each tone that has one; the default tone keeps the card's. Every other tone has an entry, so one
 *  added to the shared Menu fails the build here until it is given its colour. */
const ROW_COLOR_BY_TONE: Readonly<Record<Exclude<NonNullable<ContextMenuActionRow["tone"]>, "default">, string>> = {
  danger: "var(--c-danger, #c62828)",
  quiet: "var(--c-text-faint, #6b6b6b)",
};
const DIVIDER_STYLE = "margin: 4px 0; border: 0; border-top: 1px solid var(--c-border-faint, rgba(0, 0, 0, 0.1))";
const HOVER_BACKGROUND = "var(--c-fill-hover, rgba(0, 0, 0, 0.06))";
const GLYPH_STYLE = "display: inline-flex; flex: none; width: 1em; justify-content: center";
/** The size a row's icon is drawn at, the shared Menu's. */
const ROW_ICON_SIZE = 14;
/** The theme contract's parts (``themes/parts.ts``), which a theme's own rules select. */
const THEME_PART_ATTR = "data-part";
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
  open: ContextMenuOpen;
  close?: () => void;
}

/** The leading columns a card keeps on every row once one row fills them. */
interface RowColumns {
  hasCheck: boolean;
  hasIcon: boolean;
}

/** A row kind this renderer has no case for. Its parameter is ``never``, so adding a kind to ``ContextMenuRow``
 *  fails the build here until the renderer draws it: no kind reaches the shared Menu's pages and not these. */
function failOnUndrawnRow(row: never): never {
  throw new Error(`the element menu cannot draw this row: ${JSON.stringify(row)}`);
}

/** The fields of each row kind this renderer reads. */
interface DrawnRowFields {
  action: "kind" | "key" | "label" | "isDisabled" | "tooltip" | "tone" | "icon" | "onSelect";
  check: "kind" | "key" | "label" | "isChecked" | "tooltip" | "onToggle";
  divider: "kind";
  submenu: "kind" | "key" | "label" | "tooltip" | "rows";
}

type MustBeNever<Field extends never> = Field;

/** Fails the build when a row of ``ContextMenuRow`` gains a field ``DrawnRowFields`` does not name. The shared
 *  Menu draws such a field at once, the row types being structurally its own, so it is drawn here first. */
export type UndrawnRowField = MustBeNever<
  {
    [Kind in ContextMenuRow["kind"]]: Exclude<keyof Extract<ContextMenuRow, { kind: Kind }>, DrawnRowFields[Kind]>;
  }[ContextMenuRow["kind"]]
>;

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
    next.setAttribute(THEME_PART_ATTR, "menu");
    next.setAttribute("style", CARD_STYLE);
    // A right-click on the card itself is handled here, so the installer yields to it (as it
    // yields to the shared Menu's sheet) rather than describing one of the card's own buttons.
    // own-context-menu: dismisses -- a right-click on the open menu's own card describes nothing
    next.addEventListener("contextmenu", (event) => event.preventDefault());
    // A card with a check row or an icon keeps that column on every row, so the labels line up.
    const columns = {
      hasCheck: rows.some((row) => row.kind === "check"),
      hasIcon: rows.some((row) => row.kind === "action" && row.icon !== undefined),
    };
    for (const row of rows) next.appendChild(rowElementOf(row, isSubmenu, columns));
    return next;
  };

  const glyphOf = (): HTMLElement => {
    const glyph = ownerDocument.createElement("span");
    glyph.setAttribute("aria-hidden", "true");
    glyph.setAttribute("style", GLYPH_STYLE);
    return glyph;
  };

  /** The button every row but a divider is: its key, label, tooltip, and the columns the card keeps. */
  const rowButtonOf = (
    row: ContextMenuActionRow | ContextMenuCheckRow | ContextMenuSubmenuRow,
    columns: RowColumns,
  ): HTMLButtonElement => {
    const button = ownerDocument.createElement("button");
    button.type = "button";
    button.setAttribute(CONTEXT_MENU_ROW_ATTR, row.key);
    button.setAttribute(THEME_PART_ATTR, "menu-item");
    button.setAttribute("style", ROW_STYLE);
    if (row.tooltip !== undefined) button.title = row.tooltip;
    if (columns.hasCheck) {
      const mark = glyphOf();
      mark.textContent = row.kind === "check" && row.isChecked ? "✓" : "";
      button.appendChild(mark);
    }
    if (columns.hasIcon) {
      const glyph = glyphOf();
      const rowIcon = row.kind === "action" ? row.icon : undefined;
      // A named icon is the shared set's; markup is the page's own, written by the page that hands the row over.
      if (typeof rowIcon === "string") glyph.innerHTML = icon(rowIcon, { size: ROW_ICON_SIZE });
      else if (rowIcon !== undefined) glyph.innerHTML = rowIcon.markup;
      button.appendChild(glyph);
    }
    const label = ownerDocument.createElement("span");
    label.setAttribute("style", "flex: 1");
    label.textContent = row.label;
    button.appendChild(label);
    // The press must not move focus off the target the edit rows act on, nor collapse the
    // selection there, as the shared Menu keeps its sheet's press from doing; the click still fires.
    button.addEventListener("mousedown", (event) => event.preventDefault());
    return button;
  };

  const dividerElementOf = (): HTMLElement => {
    const divider = ownerDocument.createElement("hr");
    divider.setAttribute(THEME_PART_ATTR, "menu-separator");
    divider.setAttribute("style", DIVIDER_STYLE);
    return divider;
  };

  /** A row a pick runs: an action or a check row. */
  const pickableElementOf = (
    row: ContextMenuActionRow | ContextMenuCheckRow,
    isInSubmenu: boolean,
    columns: RowColumns,
  ): HTMLElement => {
    const button = rowButtonOf(row, columns);
    button.setAttribute("role", row.kind === "check" ? "menuitemcheckbox" : "menuitem");
    const isDisabled = row.kind === "action" && row.isDisabled === true;
    if (isDisabled) {
      button.setAttribute("style", DISABLED_ROW_STYLE);
      button.setAttribute("aria-disabled", "true");
    }
    const tone = row.kind === "action" ? row.tone : undefined;
    const toneColor = tone === undefined || tone === "default" ? undefined : ROW_COLOR_BY_TONE[tone];
    if (!isDisabled && toneColor !== undefined) button.style.color = toneColor;
    if (row.kind === "check") button.setAttribute("aria-checked", row.isChecked ? "true" : "false");
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

  const submenuElementOf = (row: ContextMenuSubmenuRow, columns: RowColumns): HTMLElement => {
    const button = rowButtonOf(row, columns);
    button.setAttribute("role", "menuitem");
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
  };

  const rowElementOf = (row: ContextMenuRow, isInSubmenu: boolean, columns: RowColumns): HTMLElement => {
    switch (row.kind) {
      case "divider":
        return dividerElementOf();
      case "action":
      case "check":
        return pickableElementOf(row, isInSubmenu, columns);
      case "submenu":
        return submenuElementOf(row, columns);
      default:
        return failOnUndrawnRow(row);
    }
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
