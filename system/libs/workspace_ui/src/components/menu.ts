/**
 * The workspace's menu: rows of options in a floating card, and the one way every menu in the
 * workspace opens, closes, and grows a submenu.
 *
 * The rules are the component's, not the caller's:
 *
 * - A MENU opens on a click and closes on a press outside it or on Escape. Nothing else closes
 *   it -- not the pointer drifting off, not a scroll, not a resize (a resize re-places it
 *   against its anchor instead). An invisible sheet sits under it while it is open, so the
 *   press that dismisses it cannot also land on a button underneath, and the surface below
 *   cannot be scrolled or hovered through it.
 * - A row may open one SUBMENU. It opens on hover (after a short intent delay) or on keyboard
 *   focus, and closes when the pointer leaves the menu-and-submenu pair -- unless the caller
 *   says it is holding unfinished work (`holdsSubmenuOpen`), in which case only a click or
 *   Escape may take it down. The pointer's trip from the row to the submenu is protected by a
 *   safe triangle, so crossing the other rows on the diagonal does not swap the submenu out
 *   from under it.
 * - A submenu cannot open another. Two levels, held by the types: a `SubmenuRow` has no
 *   `submenu` kind.
 *
 * Callers describe rows (`MenuRow`) and get the whole behaviour; a `custom` row is the escape
 * hatch for content that is not a plain row (a slider, a switch, a search field). The chrome is
 * exported for that custom content, so it can dress like the rows around it.
 *
 * Everything portals to <body>: menus open from inside dockview's clipping overlays and from
 * inside modals, and a card that extends past its panel would otherwise be cut off at the
 * panel's edge. They sit on `--z-popover`, above the modal overlays, so one opened from a
 * modal paints over it.
 *
 * The Tailwind scanner reads utility names from the literals in this file: keep every utility
 * name a contiguous literal.
 */

import m from "mithril";

import { hoverTooltipAttrs } from "./hoverTooltip";
import { icon, type IconName } from "./icons";
import {
  isInSafeTriangle,
  MENU_MARGIN,
  placeMenu,
  placeSubmenu,
  type MenuAlign,
  type MenuAnchor,
  type MenuPlacement,
  type MenuPoint,
} from "../menu-position";
import { Portal } from "../portal";

// Chrome

/** The floating card. Positioning is the caller's -- `fixed` here, `absolute` for a card that
 *  lives in its parent -- along with min-width and text size. */
export function menuCardClass(extra = ""): string {
  const parts = ["z-(--z-popover) rounded-xl border border-default bg-surface py-1 shadow-overlay"];
  if (extra !== "") parts.push(extra);
  return parts.join(" ");
}

export interface MenuRowOptions {
  /** 4px row gap instead of the default 8px. */
  tightGap?: boolean;
  /** A row that highlights but does not act (e.g. a read-only value): default arrow instead
   *  of the pointer. */
  inert?: boolean;
  extra?: string;
}

/** The keyboard-focus treatment for a focusable row (a real <button>). Inset so the ring stays
 *  inside the card instead of overhanging it. */
export const MENU_ROW_FOCUS = "focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent";

/** The row's highlight: a slab inset 4px from the card's edges, with the 12px radius the
 *  card's own 16px corner leaves 4px in, so the highlight looks concentric with the card. The
 *  width is spelled out because `w-full` adds the 8px of margin and overflows the card, and a
 *  <button> left to size itself stops short of a trailing tick. `px-2` completes the 12px the
 *  text gets from the card's edge. */
export const MENU_ROW_SLAB = "mx-1 w-[calc(100%-0.5rem)] rounded-[12px] px-2";

function rowGapClass(tightGap: boolean | undefined): string {
  return tightGap === true ? "gap-1" : "gap-2";
}

export function menuRowClass(options: MenuRowOptions = {}): string {
  const parts = [
    `flex h-8 items-center ${MENU_ROW_SLAB} text-left hover:bg-fill-hover ${MENU_ROW_FOCUS}`,
    options.inert === true ? "cursor-default" : "cursor-pointer",
    rowGapClass(options.tightGap),
  ];
  if (options.extra !== undefined && options.extra !== "") parts.push(options.extra);
  return parts.join(" ");
}

/** The rule between two row groups. Full-bleed, since the card pads only vertically. */
export function menuDividerClass(): string {
  return "my-1 border-t border-default";
}

/** Text truncated from the FRONT, for a value whose tail is the part that tells one row from
 *  another (a model path like `openrouter/qwen/qwen-2.5-72b-instruct`).
 *
 *  `direction: rtl` is what moves the ellipsis: it puts the line's END at the LEFT, which is
 *  the side the box overflows, and text too long to fit hangs its beginning off there. The
 *  text goes in a `<bdi>` so it stays one isolated left-to-right run -- without the isolation a
 *  name ending in a neutral character, `Sonnet 4.5 (thinking)` say, would have its bracket
 *  reordered to the far left. `text-align: left` is for text that DOES fit, which an rtl box
 *  would otherwise push against its right edge. */
export const START_TRUNCATED_CLASS = "truncate text-left [direction:rtl]";

export function startTruncated(text: string, extra = ""): m.Vnode {
  return m(
    "span",
    { class: extra === "" ? START_TRUNCATED_CLASS : `${START_TRUNCATED_CLASS} ${extra}` },
    m("bdi", text),
  );
}

// The rows

/** A row's leading glyph: one of the shared icons by name, or ready-made SVG markup for a
 *  glyph the shared set does not carry (a project's squiggle, an app's icon). */
export type MenuRowIcon = IconName | { markup: string };

/** How an action row reads: the ordinary row, a destructive one styled apart so it is not the
 *  easy row to reach, or a quiet one for a secondary verb ("New project" under the list). */
export type MenuRowTone = "default" | "danger" | "quiet";

interface MenuRowBase {
  /** A stable hook (`data-menu-row`) so a test can address a row by what it is. */
  key?: string;
  /** Shown on hover, above the row. */
  tooltip?: string;
}

/** A row that does one thing when picked, and closes the menu. */
export interface ActionRow extends MenuRowBase {
  kind: "action";
  label: string;
  icon?: MenuRowIcon;
  tone?: MenuRowTone;
  /** Greys the row and ignores its clicks. aria-disabled rather than a native `disabled`, so
   *  `tooltip` stays reachable to say why. */
  isDisabled?: boolean;
  /** Content after the label: a check on the current item, a control of the row's own. */
  trailing?: m.Children;
  /** 4px gap between glyph and label instead of 8px, for rows whose glyph is a small square. */
  tightGap?: boolean;
  /** Extra classes on the row, when a caller's rows need to match a neighbour outside the
   *  menu. */
  extraClass?: string;
  /** The box the icon sits in, when the default 16px square is the wrong size for the glyph. */
  iconBoxClass?: string;
  /** Leave the menu up after the pick, for an action that reports back into the menu. */
  keepsOpen?: boolean;
  onSelect: () => void;
}

/** A row that states a value and opens a submenu to change it. */
export interface SubmenuRow extends MenuRowBase {
  kind: "submenu";
  key: string;
  label: string;
  /** The current choice, stated on the row itself. */
  value?: string;
  /** A quieter qualifier after the value -- "(Claude Code)" after a provider. */
  sub?: string;
  /** `start` for a value that is a path, whose tail is the part that tells it apart. */
  truncateValue?: "start" | "end";
  /** Run when the submenu opens, hover or click alike. */
  onOpen?: () => void;
  /** The submenu's width; the menu's own when unset. */
  width?: number;
  /** The tallest the submenu may be before it scrolls. Ten rows when unset. */
  maxHeight?: number;
  /** Extra classes on the submenu card. */
  extraClass?: string;
  /** The submenu's rows -- or, for content that is not plain rows, `content`. One of the two. */
  rows?: () => readonly SubmenuChildRow[];
  content?: () => m.Children;
}

/** A row that states a value and offers nothing to do about it here. Keeps the hover
 *  highlight, because a row that does not react at all reads as broken rather than as fixed. */
export interface ValueRow extends MenuRowBase {
  kind: "value";
  label: string;
  value: string;
  sub?: string;
  truncateValue?: "start" | "end";
}

/** A row with a checkbox. Toggling it does NOT close the menu -- several can be toggled in one
 *  visit. */
export interface CheckRow extends MenuRowBase {
  kind: "check";
  label: string;
  icon?: MenuRowIcon;
  isChecked: boolean;
  onToggle: () => void;
}

export interface DividerRow {
  kind: "divider";
}

/** Anything that is not a plain row. The menu wraps it in a row slot that takes the hover like
 *  every other row, and otherwise leaves it alone -- closing the menu from inside it is the
 *  content's own call. */
export interface CustomRow {
  kind: "custom";
  key: string;
  render: () => m.Children;
}

/** What a submenu may hold: every row but another submenu. */
export type SubmenuChildRow = ActionRow | ValueRow | CheckRow | DividerRow | CustomRow;
export type MenuRow = SubmenuChildRow | SubmenuRow;

// The menu

/** Marks the trigger, the sheet, the menu and the submenu, so a test can find each by what it
 *  is: `[data-menu-part="menu"]`. */
export const MENU_PART_ATTR = "data-menu-part";
/** Marks a row by its `key`: `[data-menu-row="providers"]`. */
export const MENU_ROW_ATTR = "data-menu-row";

/** How long the pointer rests on a row before that row's submenu opens. Near-instant, but not
 *  zero, so a flick across two or three rows does not flash their submenus up in turn. */
const SUBMENU_HOVER_DELAY_MS = 40;

/** How long the safe triangle survives once the pointer is inside it: longer than the crossing
 *  takes, and short enough that a pointer parked in the wedge lets the row under it open its
 *  own submenu. */
const SAFE_TRIANGLE_GRACE_MS = 400;

/** How long an open submenu survives the pointer leaving the menu-and-submenu pair. Long
 *  enough to forgive the seam between the two boxes and a corner clipped on the way across,
 *  short enough that a submenu does not sit over the page once the pointer has gone somewhere
 *  else entirely. */
const SUBMENU_LEAVE_DELAY_MS = 220;

/** The submenu tucks 5px under the menu's edge -- the card's 1px border plus the 4px the row
 *  highlight is inset by -- so it meets the opening row's highlight and reads as that row
 *  continuing sideways. */
const SUBMENU_OVERLAP = 5;

/** The distance from a submenu's outer top edge to the top of its first row: the card's 1px
 *  border plus its 4px vertical padding. */
const SUBMENU_PADDING = 5;

/** A row is 32px tall. Ten of them before a submenu scrolls: fewer reads as a keyhole, many
 *  more as a wall. */
const MENU_ROW_HEIGHT = 32;
const SUBMENU_DEFAULT_MAX_HEIGHT = 2 * SUBMENU_PADDING + 10 * MENU_ROW_HEIGHT;

/** The invisible sheet under an open menu. Same layer as the menu, which stays on top of it by
 *  rendering after it as a sibling. `menu-sheet` is a bare marker with no CSS attached. */
const SHEET_CLASS = "menu-sheet fixed inset-0 z-(--z-popover)";

const CARD_TEXT = "text-(length:--font-size-row)";

/** The 12px the calc subtracts is `MENU_MARGIN` on each side: a menu taller than the window
 *  scrolls between the margins rather than running off it. */
const MENU_CARD_CLASS = menuCardClass(
  `fixed max-h-[calc(100vh-12px)] overflow-y-auto overscroll-contain ${CARD_TEXT}`,
);
/** The flex column caps the scroll region beneath a pinned head (a search field). */
const SUBMENU_CARD_CLASS = menuCardClass(`fixed flex flex-col overflow-hidden ${CARD_TEXT}`);

const ICON_BOX_CLASS = "flex w-4 shrink-0 items-center justify-center";
const ROW_LABEL_CLASS = "min-w-0 flex-1 truncate";
/** A submenu row's or value row's label sits one colour step back from its value. */
const KEY_LABEL_CLASS = "text-secondary";
const KEY_VALUE_CLASS = "ml-auto flex min-w-0 items-center gap-1.5";
const KEY_SUB_CLASS = "type-helper text-faint";
const KEY_CHEVRON_CLASS = "shrink-0 text-faint";

const TONE_CLASS: Record<MenuRowTone, string> = {
  default: "text-primary",
  danger: "text-danger",
  quiet: "text-faint hover:text-primary",
};
const DISABLED_CLASS = "text-faint cursor-default hover:bg-transparent";

/** The button inside a row that carries `trailing`: the slab, the hover and the tone live on
 *  the row's wrapper, so this is only the label's own line and its focus ring. */
const ROW_INNER_BUTTON_CLASS = `flex h-full min-w-0 flex-1 items-center text-left ${MENU_ROW_FOCUS}`;

export interface MenuOptions {
  /** Which side of its anchor the menu hangs on. */
  placement: MenuPlacement;
  /** Which of the anchor's edges the menu's own lines up with. `start` when unset. */
  align?: MenuAlign;
  /** A fixed width. Unset, the menu sizes to its content (with `minWidth` as a floor). */
  width?: number;
  minWidth?: number;
  /** Extra classes on the menu card -- a bare marker for tests, most usefully. */
  extraClass?: string;
  /** `menu` when unset. `dialog` for a card that holds a picker rather than a list of verbs. */
  role?: "menu" | "dialog";
  onOpen?: () => void;
  onClose?: () => void;
  /** Run whenever the open submenu changes, with the new key or null: where state scoped to a
   *  submenu -- an armed confirmation, a search query -- is reset. */
  onSubmenuChange?: (key: string | null) => void;
  /** Whether `key`'s open submenu is holding unfinished work -- a half-typed rename, an armed
   *  confirmation, a search query. While it answers true, hover cannot take the submenu down
   *  or swap it for another row's; a click, Escape, and the menu closing still can. */
  holdsSubmenuOpen?: (key: string) => boolean;
  /** How to repaint after the menu changes state on its own (a timer, a key press). Mithril's
   *  own redraw when unset, which is right for a menu rendered inside a mounted tree; a menu
   *  rendered into an `m.render` root of its own re-renders that root here instead. */
  redraw?: () => void;
}

export interface Menu {
  isOpen(): boolean;
  /** Whether `key`'s submenu is the open one. */
  isSubmenuOpen(key: string): boolean;
  /** Open against the anchor's box -- or against the anchor ELEMENT itself, which is better
   *  when the caller has it: a window resize re-measures the element and the menu follows
   *  it. */
  open(anchor: MenuAnchor | HTMLElement): void;
  close(): void;
  toggle(anchor: MenuAnchor | HTMLElement): void;
  /** Close the open submenu and leave the menu up: for a pick inside a submenu that is done
   *  but leaves the menu with something still worth adjusting. */
  closeSubmenu(): void;
  /** Close, and drop every listener and timer. Call from the owning component's `onremove`:
   *  a menu still open when its owner unmounts would otherwise keep its Escape listener on the
   *  window for good. */
  dispose(): void;
  /** Spread onto the element that opens the menu: the click toggles it at that element. */
  triggerAttrs(): m.Attributes;
  /** The open menu -- sheet, card and submenu -- portalled to <body>; null when closed. For a
   *  caller rendering inside a mounted mithril tree. */
  view(rows: readonly MenuRow[]): m.Children;
  /** The same, unportalled, for a caller with an `m.render` root of its own. */
  render(rows: readonly MenuRow[]): m.Children;
}

export function createMenu(options: MenuOptions): Menu {
  const redraw = options.redraw ?? ((): void => m.redraw());

  // Where the menu sits, captured when it opens. Open iff non-null.
  let anchor: MenuAnchor | null = null;
  // The element the menu hangs off, when the caller gave one rather than a box.
  let anchorElement: HTMLElement | null = null;
  // The menu card's box once placed: what the safe triangle reads the submenu's near edge
  // against.
  let menuRect: DOMRect | null = null;

  let openSubmenu: string | null = null;
  // The top of the row that opened it, which its first row lines up with.
  let submenuRowTop = 0;
  // The submenu's box once placed, and re-measured on every redraw that changes it.
  let submenuRect: DOMRect | null = null;
  // The height a scrolling submenu is pinned at while it is open; null while it has never
  // filled its cap.
  let submenuLockedHeight: number | null = null;

  // The safe triangle's apex: the pointer's last position on the row whose submenu is open,
  // and the clock that lets the wedge go once the pointer has parked inside it.
  let safeApex: MenuPoint | null = null;
  let safeApexTimer: number | null = null;
  // The row a hover is counting down on, and the count.
  let hoverIntentRow: HTMLElement | null = null;
  let hoverIntentTimer: number | null = null;
  // The count between the pointer leaving the menu-and-submenu pair and the submenu closing.
  // The two are separate boxes, so crossing between them fires a leave before the matching
  // enter -- the delay is what stops that seam reading as a departure.
  let stackLeaveTimer: number | null = null;

  function cancelHoverIntent(): void {
    if (hoverIntentTimer !== null) {
      window.clearTimeout(hoverIntentTimer);
      hoverIntentTimer = null;
    }
    hoverIntentRow = null;
  }

  function cancelStackLeave(): void {
    if (stackLeaveTimer !== null) {
      window.clearTimeout(stackLeaveTimer);
      stackLeaveTimer = null;
    }
  }

  function clearSafeApex(): void {
    if (safeApexTimer !== null) {
      window.clearTimeout(safeApexTimer);
      safeApexTimer = null;
    }
    safeApex = null;
  }

  /** Whether hover may change the open submenu right now. It may not while the caller has
   *  unfinished work in it: a drifting pointer must not throw away something the user is
   *  mid-way through typing or arming. */
  function isSubmenuHeld(): boolean {
    return openSubmenu !== null && options.holdsSubmenuOpen?.(openSubmenu) === true;
  }

  /** The ONLY way the open submenu changes, so what is scoped to a submenu is cleared by
   *  construction rather than by remembering to at every site. */
  function setSubmenu(next: string | null): void {
    if (next !== openSubmenu) {
      // A new submenu is a new box in a new place: the old one's measurements, and any trip
      // towards it, describe something that is gone.
      clearSafeApex();
      submenuRect = null;
      submenuLockedHeight = null;
      openSubmenu = next;
      options.onSubmenuChange?.(next);
    }
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key !== "Escape" || anchor === null) return;
    // Ours alone: a menu open inside a modal is the thing Escape closes, not the modal under it.
    event.stopImmediatePropagation();
    event.preventDefault();
    close();
  }

  /** The window has changed shape: the menu keeps its anchor rather than closing, so
   *  re-measure the anchor (when it is an element that is still on the page) and let the
   *  redraw re-place the card. */
  function onResize(): void {
    if (anchorElement !== null && anchorElement.isConnected) {
      const rect = anchorElement.getBoundingClientRect();
      anchor = { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width };
    }
    redraw();
  }

  function open(next: MenuAnchor | HTMLElement): void {
    if (anchor === null) {
      window.addEventListener("keydown", onKeydown, true);
      window.addEventListener("resize", onResize);
    }
    anchorElement = next instanceof HTMLElement ? next : null;
    const rect = next instanceof HTMLElement ? next.getBoundingClientRect() : next;
    anchor = { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width };
    setSubmenu(null);
    options.onOpen?.();
    redraw();
  }

  function close(): void {
    if (anchor === null) return;
    window.removeEventListener("keydown", onKeydown, true);
    window.removeEventListener("resize", onResize);
    anchor = null;
    anchorElement = null;
    menuRect = null;
    cancelHoverIntent();
    cancelStackLeave();
    setSubmenu(null);
    options.onClose?.();
    redraw();
  }

  /** The pointer has left the menu or the submenu: a pending hover no longer opens, and an
   *  open submenu closes once its grace runs out. */
  function handleStackLeave(): void {
    cancelHoverIntent();
    if (openSubmenu === null) return;
    cancelStackLeave();
    stackLeaveTimer = window.setTimeout(() => {
      stackLeaveTimer = null;
      cancelHoverIntent();
      // Checked when the grace runs out, not when the pointer left: it is the work's state
      // at closing time that decides whether closing would lose anything.
      if (isSubmenuHeld()) return;
      setSubmenu(null);
      redraw();
    }, SUBMENU_LEAVE_DELAY_MS);
  }

  /** Whether the pointer is on its way to the submenu that is already open, rather than
   *  changing its mind about which row it wants. */
  function isTravellingToSubmenu(point: MenuPoint): boolean {
    if (openSubmenu === null || safeApex === null || submenuRect === null || menuRect === null) return false;
    // Which of the submenu's edges faces the menu -- `placeSubmenu` puts it on either side.
    // Read off the two boxes' centres rather than off the near edges, which overlap by design.
    const trailing = submenuRect.left + submenuRect.width / 2 > menuRect.left + menuRect.width / 2;
    return isInSafeTriangle(point, safeApex, {
      edgeX: trailing ? submenuRect.left : submenuRect.right,
      top: submenuRect.top,
      bottom: submenuRect.bottom,
    });
  }

  /** Show `next`'s submenu (or none) because the pointer or the keyboard settled on its row. */
  function openSubmenuFromRow(next: SubmenuRow | null, row: HTMLElement): void {
    const key = next === null ? null : next.key;
    if (key === openSubmenu) return;
    if (next !== null) {
      submenuRowTop = row.getBoundingClientRect().top;
    }
    setSubmenu(key);
    next?.onOpen?.();
  }

  /** Every menu row's pointer and keyboard handling, in one recipe.
   *
   *  `submenu` is the submenu the row opens, or null for a row that opens nothing -- and a row
   *  that opens nothing still takes the hover, closing whatever is open.
   *
   *  `mousemove` rather than `mouseenter`: it keeps the safe triangle's apex on the pointer's
   *  actual last position over the owning row instead of on wherever it first crossed the
   *  edge, and a row entered THROUGH the triangle -- protected, so it opened nothing -- gets
   *  another chance as soon as the pointer moves off the wedge, which a single enter event
   *  cannot give it. */
  function hoverRowAttrs(submenu: SubmenuRow | null): m.Attributes {
    return {
      onmousemove: (event: MouseEvent) => {
        const row = event.currentTarget as HTMLElement;
        const point: MenuPoint = { x: event.clientX, y: event.clientY };
        // On the row whose submenu is up: every move here restarts the trip, so the apex is
        // the pointer's true exit point and the grace clock only ever runs on a trip that has
        // actually set off.
        if (submenu !== null && submenu.key === openSubmenu) {
          clearSafeApex();
          safeApex = point;
          cancelHoverIntent();
          return;
        }
        if (submenu === null && openSubmenu === null) {
          // Nothing to open and nothing to close -- but another row's rest may still be
          // counting down, and it must not open a submenu the pointer has already moved on
          // from.
          cancelHoverIntent();
          return;
        }
        if (isTravellingToSubmenu(point)) {
          // Started across. Arriving cancels this (the submenu's own `mouseenter`); parking in
          // the wedge instead lets it run out, so the row underneath gets its turn.
          if (safeApexTimer === null) {
            safeApexTimer = window.setTimeout(() => {
              safeApexTimer = null;
              safeApex = null;
            }, SAFE_TRIANGLE_GRACE_MS);
          }
          return;
        }
        // Already counting down on this very row; restarting the clock would mean a pointer
        // that keeps twitching never opens anything.
        if (hoverIntentRow === row) return;
        cancelHoverIntent();
        hoverIntentRow = row;
        hoverIntentTimer = window.setTimeout(() => {
          hoverIntentTimer = null;
          hoverIntentRow = null;
          if (isSubmenuHeld()) return;
          openSubmenuFromRow(submenu, row);
          redraw();
        }, SUBMENU_HOVER_DELAY_MS);
      },
      // Keyboard focus opens immediately: there is no aiming to wait for. `focusin` bubbles,
      // so a row whose focusable part is a child is covered too. `:focus-visible` keeps a
      // mouse click out of this path: it focuses the row as well, and opening from here would
      // race the click's own toggle.
      onfocusin: (event: FocusEvent) => {
        const focused = event.target as HTMLElement | null;
        if (focused === null || !focused.matches(":focus-visible")) return;
        cancelHoverIntent();
        openSubmenuFromRow(submenu, event.currentTarget as HTMLElement);
        // `focusin` is not one of the events a portal host redraws for.
        redraw();
      },
    };
  }

  function tooltipAttrs(text: string | undefined): m.Attributes {
    return text === undefined ? {} : hoverTooltipAttrs(text, "above");
  }

  function rowKeyAttr(key: string | undefined): m.Attributes {
    return key === undefined ? {} : { [MENU_ROW_ATTR]: key };
  }

  function iconBox(rowIcon: MenuRowIcon | undefined, boxClass: string): m.Children {
    if (rowIcon === undefined) return null;
    const markup = typeof rowIcon === "string" ? icon(rowIcon, { size: 14 }) : rowIcon.markup;
    return m("span", { class: boxClass }, m.trust(markup));
  }

  function valueText(value: string | undefined, truncate: "start" | "end" | undefined): m.Children {
    if (value === undefined) return null;
    return truncate === "start" ? startTruncated(value) : m("span", { class: "truncate" }, value);
  }

  function actionRow(row: ActionRow, hover: m.Attributes): m.Vnode {
    const isDisabled = row.isDisabled === true;
    const tone = isDisabled ? DISABLED_CLASS : TONE_CLASS[row.tone ?? "default"];
    const rowClass = menuRowClass({ tightGap: row.tightGap, extra: `${tone} ${row.extraClass ?? ""}`.trim() });
    const label = [
      iconBox(row.icon, row.iconBoxClass ?? ICON_BOX_CLASS),
      m("span", { class: ROW_LABEL_CLASS }, row.label),
    ];
    const buttonAttrs: m.Attributes = {
      type: "button",
      role: "menuitem",
      "aria-disabled": isDisabled ? "true" : undefined,
      onclick: (event: MouseEvent) => {
        event.stopPropagation();
        if (isDisabled) return;
        row.onSelect();
        if (row.keepsOpen !== true) close();
      },
    };
    if (row.trailing === undefined || row.trailing === null) {
      return m(
        "button",
        { ...buttonAttrs, class: rowClass, ...rowKeyAttr(row.key), ...tooltipAttrs(row.tooltip), ...hover },
        label,
      );
    }
    // Trailing content rides BESIDE the button rather than inside it: buttons cannot nest,
    // and a trailing control may be a button of its own. The wrapper takes the slab, the
    // hover and the tone, so the row still lights as one piece under either.
    return m("div", { class: rowClass, ...rowKeyAttr(row.key), ...tooltipAttrs(row.tooltip), ...hover }, [
      m(
        "button",
        {
          ...buttonAttrs,
          class: `${ROW_INNER_BUTTON_CLASS} ${isDisabled ? "cursor-default" : "cursor-pointer"} ${rowGapClass(row.tightGap)}`,
        },
        label,
      ),
      row.trailing,
    ]);
  }

  function submenuRow(row: SubmenuRow): m.Vnode {
    const isOpen = openSubmenu === row.key;
    return m(
      "button",
      {
        type: "button",
        role: "menuitem",
        class: menuRowClass({ extra: TONE_CLASS.default }),
        // The chevron is decoration to a screen reader, so the row says out loud that it is a
        // disclosure.
        "aria-haspopup": "true",
        "aria-expanded": isOpen ? "true" : "false",
        ...rowKeyAttr(row.key),
        ...tooltipAttrs(row.tooltip),
        ...hoverRowAttrs(row),
        // Click still toggles: on a touch screen there is no hover to intend anything with.
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          cancelHoverIntent();
          openSubmenuFromRow(isOpen ? null : row, event.currentTarget as HTMLElement);
          redraw();
        },
      },
      [
        m("span", { class: KEY_LABEL_CLASS }, row.label),
        m("span", { class: KEY_VALUE_CLASS }, [
          valueText(row.value, row.truncateValue),
          row.sub === undefined ? null : m("span", { class: KEY_SUB_CLASS }, `(${row.sub})`),
          m("span", { class: KEY_CHEVRON_CLASS }, m.trust(icon("chevron-right", { size: 13 }))),
        ]),
      ],
    );
  }

  function valueRow(row: ValueRow, hover: m.Attributes): m.Vnode {
    return m(
      "div",
      {
        role: "menuitem",
        "aria-disabled": "true",
        class: menuRowClass({ inert: true, extra: TONE_CLASS.default }),
        ...rowKeyAttr(row.key),
        ...tooltipAttrs(row.tooltip),
        ...hover,
      },
      [
        m("span", { class: KEY_LABEL_CLASS }, row.label),
        m("span", { class: KEY_VALUE_CLASS }, [
          valueText(row.value, row.truncateValue),
          row.sub === undefined ? null : m("span", { class: KEY_SUB_CLASS }, `(${row.sub})`),
        ]),
      ],
    );
  }

  function checkRow(row: CheckRow, hover: m.Attributes): m.Vnode {
    return m(
      "label",
      {
        class: menuRowClass({ extra: TONE_CLASS.default }),
        ...rowKeyAttr(row.key),
        ...tooltipAttrs(row.tooltip),
        ...hover,
      },
      [
        m("span", { class: "relative flex h-4 w-4 shrink-0 items-center justify-center" }, [
          m("input", {
            type: "checkbox",
            checked: row.isChecked,
            onchange: () => row.onToggle(),
            class:
              "absolute inset-0 m-0 h-4 w-4 cursor-pointer appearance-none rounded border " +
              (row.isChecked ? "border-accent bg-accent" : "border-default bg-surface"),
          }),
          row.isChecked
            ? m(
                "span",
                { class: "pointer-events-none relative text-white" },
                m.trust(icon("check", { size: 11, strokeWidth: 3 })),
              )
            : null,
        ]),
        iconBox(row.icon, "text-faint flex w-5 shrink-0 items-center justify-center"),
        row.label,
      ],
    );
  }

  function renderRow(row: MenuRow, inSubmenu: boolean): m.Children {
    // Rows inside a submenu take no hover: there is nothing further to open, and the submenu's
    // own leave handling is what closes it.
    const hover = inSubmenu ? {} : hoverRowAttrs(null);
    switch (row.kind) {
      case "action":
        return actionRow(row, hover);
      case "submenu":
        return submenuRow(row);
      case "value":
        return valueRow(row, hover);
      case "check":
        return checkRow(row, hover);
      case "divider":
        return m("div", { role: "separator", class: menuDividerClass() });
      case "custom":
        return m("div", { ...rowKeyAttr(row.key), ...hover }, row.render());
    }
  }

  function menuCard(rows: readonly MenuRow[]): m.Vnode {
    const at = anchor as MenuAnchor;
    const place = (vnode: m.VnodeDOM): void => {
      const element = vnode.dom as HTMLElement;
      const rect = element.getBoundingClientRect();
      const position = placeMenu(
        at,
        { width: rect.width, height: rect.height },
        { width: window.innerWidth, height: window.innerHeight },
        options.placement,
        options.align,
      );
      element.style.left = `${position.left}px`;
      element.style.top = `${position.top}px`;
      menuRect = element.getBoundingClientRect();
      // The rows move with the card (a resize re-places it), so an open submenu's alignment
      // follows its row's CURRENT top, not where the row was when it opened.
      if (openSubmenu !== null) {
        const owner = element.querySelector<HTMLElement>(`[${MENU_ROW_ATTR}="${CSS.escape(openSubmenu)}"]`);
        if (owner !== null) submenuRowTop = owner.getBoundingClientRect().top;
      }
    };
    const sizing =
      options.width !== undefined
        ? `width: ${options.width}px;`
        : options.minWidth !== undefined
          ? `min-width: ${options.minWidth}px;`
          : "";
    return m(
      "div",
      {
        class: options.extraClass === undefined ? MENU_CARD_CLASS : `${MENU_CARD_CLASS} ${options.extraClass}`,
        role: options.role ?? "menu",
        [MENU_PART_ATTR]: "menu",
        style: `left: 0; top: 0; ${sizing}`,
        oncreate: place,
        onupdate: place,
        onmouseenter: cancelStackLeave,
        onmouseleave: handleStackLeave,
      },
      rows.map((row) => renderRow(row, false)),
    );
  }

  function submenuCard(row: SubmenuRow): m.Vnode {
    const width = row.width ?? options.width;
    const maxHeight = row.maxHeight ?? SUBMENU_DEFAULT_MAX_HEIGHT;
    // The safe triangle's base is this box, and its height follows its contents -- so measure
    // on arrival AND on every redraw that changes them: a triangle pointing at the box's old
    // bottom would guard rows nobody is heading through.
    const place = (vnode: m.VnodeDOM): void => {
      const element = vnode.dom as HTMLElement;
      if (submenuLockedHeight === null) element.style.height = "";
      const box = menuRect ?? element.getBoundingClientRect();
      const size = element.getBoundingClientRect();
      const placed = placeSubmenu({
        menuLeft: box.left,
        menuWidth: box.width,
        rowTop: submenuRowTop,
        submenuPadding: SUBMENU_PADDING,
        contentHeight: size.height,
        submenuWidth: size.width,
        maxSubmenuHeight: maxHeight,
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
        margin: MENU_MARGIN,
        overlap: SUBMENU_OVERLAP,
      });
      // A submenu that has filled its cap keeps that height for as long as it stays open: a
      // box that shrank with a filtered list would be re-placed lower, sliding the rows being
      // aimed at out from under the pointer mid-keystroke. The -1 forgives sub-pixel rounding
      // in the measurement.
      if (submenuLockedHeight === null && size.height >= placed.maxHeight - 1) {
        submenuLockedHeight = placed.maxHeight;
      }
      element.style.left = `${placed.left}px`;
      element.style.top = `${placed.top}px`;
      element.style.maxHeight = `${placed.maxHeight}px`;
      if (submenuLockedHeight !== null) element.style.height = `${submenuLockedHeight}px`;
      submenuRect = element.getBoundingClientRect();
    };
    const content =
      row.content !== undefined ? row.content() : (row.rows ?? (() => []))().map((child) => renderRow(child, true));
    return m(
      "div",
      {
        class: row.extraClass === undefined ? SUBMENU_CARD_CLASS : `${SUBMENU_CARD_CLASS} ${row.extraClass}`,
        role: "menu",
        [MENU_PART_ATTR]: "submenu",
        style: `left: 0; top: 0; ${width === undefined ? "min-width: 180px;" : `width: ${width}px;`} max-height: ${maxHeight}px;`,
        oncreate: place,
        onupdate: place,
        // Arrived: the trip is over, so the wedge that protected it lets the menu's rows
        // answer the pointer normally again.
        onmouseenter: () => {
          cancelStackLeave();
          cancelHoverIntent();
          clearSafeApex();
        },
        onmouseleave: handleStackLeave,
      },
      content,
    );
  }

  function sheet(): m.Vnode {
    return m("div", {
      class: SHEET_CLASS,
      [MENU_PART_ATTR]: "sheet",
      // Mouse DOWN, not click: a click fires wherever the press ENDED, and a press that starts
      // on the menu and is released past its edge is not a dismissal. preventDefault keeps the
      // press from moving focus or starting a selection.
      onmousedown: (event: MouseEvent) => {
        event.preventDefault();
        event.stopPropagation();
        close();
      },
    });
  }

  function render(rows: readonly MenuRow[]): m.Children {
    if (anchor === null) return null;
    const submenu =
      openSubmenu === null
        ? null
        : (rows.find((row): row is SubmenuRow => row.kind === "submenu" && row.key === openSubmenu) ?? null);
    return [sheet(), menuCard(rows), submenu === null ? null : submenuCard(submenu)];
  }

  function toggle(next: MenuAnchor | HTMLElement): void {
    if (anchor === null) open(next);
    else close();
  }

  return {
    isOpen: () => anchor !== null,
    isSubmenuOpen: (key: string) => openSubmenu === key,
    open,
    close,
    toggle,
    closeSubmenu(): void {
      cancelHoverIntent();
      cancelStackLeave();
      setSubmenu(null);
      redraw();
    },
    dispose(): void {
      close();
      // A hover still counting down, or a submenu still on its way out, would otherwise fire
      // into a component that is gone and redraw it.
      cancelHoverIntent();
      cancelStackLeave();
      clearSafeApex();
    },
    triggerAttrs(): m.Attributes {
      return {
        [MENU_PART_ATTR]: "trigger",
        "aria-haspopup": options.role === "dialog" ? "dialog" : "menu",
        "aria-expanded": anchor !== null ? "true" : "false",
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          toggle(event.currentTarget as HTMLElement);
        },
      };
    },
    view(rows: readonly MenuRow[]): m.Children {
      if (anchor === null) return null;
      return m(Portal, { children: render(rows) });
    },
    render,
  };
}
