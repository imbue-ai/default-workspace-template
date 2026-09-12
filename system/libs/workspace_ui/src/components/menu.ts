/**
 * The workspace's menu: rows of options in a floating card, and the one way every menu in the
 * workspace opens, closes, and grows a submenu.
 *
 * The rules are the component's, not the caller's:
 *
 * - A MENU opens on a click and closes on a press outside it or on Escape. Nothing else closes
 *   it -- not the pointer drifting off, not a scroll, not a resize. An invisible sheet sits
 *   under it while it is open, so the press that dismisses it cannot also land on a button
 *   underneath, and the surface below cannot be scrolled or hovered through it.
 * - A row may open one SUBMENU. It opens on hover (after a short intent delay) or on keyboard
 *   focus, and closes when the pointer leaves the menu-and-submenu pair. The pointer's trip
 *   from the row to the submenu is protected by a safe triangle, so crossing the other rows on
 *   the diagonal does not swap the submenu out from under it.
 * - A submenu cannot open another. Two levels, held by the types: a `SubmenuRow` has no
 *   `submenu` kind.
 *
 * Callers describe rows (`MenuRow`) and get the whole behaviour; a `custom` row is the escape
 * hatch for content that is not a plain row (a slider, a switch, a search field). The chrome --
 * `menuCardClass`, `menuRowClass`, `menuDividerClass` -- is exported for that custom content
 * so it can dress like the rows around it.
 *
 * Everything portals to <body>: menus open from inside dockview's clipping overlays and from
 * inside modals, and a card that extends past its panel would otherwise be cut off at the
 * panel's edge. They sit on `--z-popover`, above the modal overlays, because a popover is the
 * most recently opened thing on screen and one opened from a modal has to paint over it.
 *
 * The Tailwind scanner reads utility names from the literals in this file (base.css's
 * `@source` covers every .ts file): keep every utility name a contiguous literal.
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

/** The floating card: the primary surface with a hairline border, the 16px radius of the
 *  workspace's largest surfaces, and the overlay elevation shadow. Positioning is the caller's
 *  -- `fixed` here, `absolute` for a card that lives in its parent -- along with min-width and
 *  text size. */
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
 *  inside the card instead of the OS default halo overhanging it. Inert on a non-focusable row,
 *  so it rides the base. */
const MENU_ROW_FOCUS = "focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent";

/** The row's highlight: a slab inset 4px from the card's edges, with the 12px radius the
 *  card's own 16px corner leaves 4px in, so the highlight looks concentric with the card.
 *  The width is explicit rather than `w-full` (which adds the 8px of margin and overflows the
 *  card) or `auto` (a <button> shrink-to-fits even at `display: flex`, and the highlight
 *  stops short of a trailing tick). `px-2` completes the 12px the text gets from the card's
 *  edge. */
const MENU_ROW_SLAB = "mx-1 w-[calc(100%-0.5rem)] rounded-[12px] px-2";

export function menuRowClass(options: MenuRowOptions = {}): string {
  const parts = [
    `flex h-8 items-center ${MENU_ROW_SLAB} text-left hover:bg-fill-hover ${MENU_ROW_FOCUS}`,
    options.inert === true ? "cursor-default" : "cursor-pointer",
    options.tightGap === true ? "gap-1" : "gap-2",
  ];
  if (options.extra !== undefined && options.extra !== "") parts.push(options.extra);
  return parts.join(" ");
}

/** The rule between two row groups. Full-bleed, since the card pads only vertically. */
export function menuDividerClass(): string {
  return "my-1 border-t border-default";
}

/** Text truncated from the FRONT. `openrouter/qwen/qwen-2.5-72b-instruct` is a path whose head
 *  repeats down a whole list and whose tail is the only part that tells one row from another,
 *  so dropping the head is the only truncation that leaves anything to read by.
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
  /** A stable hook (`data-menu-row`) so a test can address a row by what it is rather than by
   *  its text or its classes. */
  key?: string;
  /** Shown on hover, above the row so the bubble never covers the rows beneath it. */
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
  /** Leave the menu up after the pick, for an action that reports back into the menu -- one
   *  whose failure is shown as a line under the rows. The default is to close: a pick is a
   *  pick. */
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
 *  highlight, because a row that does not react at all reads as broken rather than as fixed;
 *  its tooltip is where it says what would change it. */
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
 *  every other row (so pointing at it closes an open submenu), and otherwise leaves it alone --
 *  closing the menu from inside it is the content's own call. */
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

/** How long the pointer rests on a row before that row's submenu opens. Near-instant, unlike
 *  the tooltip's 250ms: a submenu is what the pointer came for. Not zero, so a flick across
 *  two or three rows does not flash their submenus up in turn. */
const SUBMENU_HOVER_DELAY_MS = 40;

/** How long the safe triangle survives once the pointer is inside it. Crossing to the submenu
 *  takes well under this; a pointer still in the wedge after it has stopped on a row, and the
 *  wedge lets go so that row can open its own submenu. */
const SAFE_TRIANGLE_GRACE_MS = 400;

/** How long an open submenu survives the pointer leaving the menu-and-submenu pair.
 *
 *  Long enough to forgive the seam between the two boxes and a corner clipped on the way
 *  across, short enough that a submenu does not sit over the page once the pointer has gone
 *  somewhere else entirely. */
const SUBMENU_LEAVE_DELAY_MS = 220;

/** The submenu tucks 5px under the menu's edge -- the card's 1px border plus the 4px the row
 *  highlight is inset by -- so it meets the opening row's highlight and reads as that row
 *  continuing sideways. */
const SUBMENU_OVERLAP = 5;

/** The distance from a submenu's outer top edge to the top of its first row: the card's 1px
 *  border plus its 4px vertical padding. What `placeSubmenu` subtracts so the first ROW lands
 *  on the opening row, not the box. */
const SUBMENU_PADDING = 5;

/** A row is 32px tall. Ten of them before a submenu scrolls: fewer and a long catalog reads
 *  as a keyhole; many more and the submenu is a wall. */
const MENU_ROW_HEIGHT = 32;
const SUBMENU_DEFAULT_MAX_HEIGHT = 2 * SUBMENU_PADDING + 10 * MENU_ROW_HEIGHT;

/** The invisible sheet under an open menu. It takes every hover and every press that is not on
 *  the menu itself, so while the menu is up nothing behind it lights up, wakes a tooltip, or
 *  receives a click. Same layer as the menu, which stays on top of it by rendering after it as
 *  a sibling. `menu-sheet` is a bare marker with no CSS attached. */
const SHEET_CLASS = "menu-sheet fixed inset-0 z-(--z-popover)";

const CARD_TEXT = "text-(length:--font-size-row)";

const MENU_CARD_CLASS = menuCardClass(`fixed ${CARD_TEXT}`);
/** The flex column caps the scroll region beneath a pinned head (a search field). */
const SUBMENU_CARD_CLASS = menuCardClass(`fixed flex flex-col overflow-hidden ${CARD_TEXT}`);

const ICON_BOX_CLASS = "flex w-4 shrink-0 items-center justify-center";
const ROW_LABEL_CLASS = "min-w-0 flex-1 truncate";
/** A submenu row's or value row's label sits one colour step back from its value, so the menu
 *  reads as a spec sheet. */
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
  /** Run whenever the open submenu changes, with the new key or null. What is scoped to a
   *  submenu -- an armed confirmation, a search query -- is reset here rather than at every
   *  site that could change the submenu. */
  onSubmenuChange?: (key: string | null) => void;
  /** How to repaint after the menu changes state on its own (a timer, a key press). Mithril's
   *  own redraw when unset, which is right for a menu rendered inside a mounted tree; a menu
   *  rendered into an `m.render` root of its own re-renders that root here instead. */
  redraw?: () => void;
}

export interface Menu {
  isOpen(): boolean;
  /** Whether `key`'s submenu is the open one. */
  isSubmenuOpen(key: string): boolean;
  open(anchor: MenuAnchor): void;
  close(): void;
  toggle(anchor: MenuAnchor): void;
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
  // The menu card's box once placed: what the safe triangle reads the submenu's near edge
  // against.
  let menuRect: DOMRect | null = null;

  let openSubmenu: string | null = null;
  // The top of the row that opened it, which its first row lines up with.
  let submenuRowTop = 0;
  // The submenu's box once placed, and re-measured on every redraw that changes it.
  let submenuRect: DOMRect | null = null;

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

  /** The ONLY way the open submenu changes, so what is scoped to a submenu is cleared by
   *  construction rather than by remembering to at every site. */
  function setSubmenu(next: string | null): void {
    if (next !== openSubmenu) {
      // A new submenu is a new box in a new place: nothing has travelled towards it yet, and
      // the old one's measurements describe a box that is gone.
      clearSafeApex();
      submenuRect = null;
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

  function open(next: MenuAnchor): void {
    if (anchor === null) {
      window.addEventListener("keydown", onKeydown, true);
    }
    anchor = { left: next.left, right: next.right, top: next.top, bottom: next.bottom, width: next.width };
    setSubmenu(null);
    options.onOpen?.();
    redraw();
  }

  function close(): void {
    if (anchor === null) return;
    window.removeEventListener("keydown", onKeydown, true);
    anchor = null;
    menuRect = null;
    cancelHoverIntent();
    cancelStackLeave();
    setSubmenu(null);
    options.onClose?.();
    redraw();
  }

  /** The pointer has left the menu or the submenu: a pending hover no longer opens, and an
   *  open submenu closes once its grace runs out, since what hover opened hover dismisses.
   *  The menu itself stays: a click opened it, and only a click or Escape closes it. */
  function handleStackLeave(): void {
    cancelHoverIntent();
    if (openSubmenu === null) return;
    cancelStackLeave();
    stackLeaveTimer = window.setTimeout(() => {
      stackLeaveTimer = null;
      cancelHoverIntent();
      setSubmenu(null);
      redraw();
    }, SUBMENU_LEAVE_DELAY_MS);
  }

  /** Whether the pointer is on its way to the submenu that is already open, rather than
   *  changing its mind about which row it wants. Only ever true while a submenu is up AND the
   *  pointer has been on the row that opened it, so the wedge cannot outlive the trip it was
   *  measured for. */
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
   *  that opens nothing still takes the hover, closing whatever is open. Within the menu the
   *  pointer decides what is showing; only outside it does a click still have to.
   *
   *  `mousemove` rather than `mouseenter`, for two reasons. It keeps the safe triangle's apex
   *  on the pointer's actual last position over the owning row instead of on wherever it first
   *  crossed the edge. And a row entered THROUGH the triangle -- protected, so it opened
   *  nothing -- gets another chance as soon as the pointer moves off the wedge, which a single
   *  enter event cannot give it. */
  function hoverRowAttrs(submenu: SubmenuRow | null): m.Attributes {
    return {
      onmousemove: (event: MouseEvent) => {
        const row = event.currentTarget as HTMLElement;
        const point: MenuPoint = { x: event.clientX, y: event.clientY };
        // On the row whose submenu is up: this is the trip's starting point, right up until
        // the pointer leaves. Every move here restarts it, so the apex is the true exit point
        // and the grace clock only ever runs on a trip that has actually set off.
        if (submenu !== null && submenu.key === openSubmenu) {
          clearSafeApex();
          safeApex = point;
          cancelHoverIntent();
          return;
        }
        if (submenu === null && openSubmenu === null) return;
        if (isTravellingToSubmenu(point)) {
          // Started across. Arriving cancels this (the submenu's own `mouseenter`); parking in
          // the wedge instead lets it run out, and the row underneath gets its turn.
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
          openSubmenuFromRow(submenu, row);
          redraw();
        }, SUBMENU_HOVER_DELAY_MS);
      },
      // Keyboard focus opens immediately -- there is no aiming to wait for, and a tab that has
      // landed on a row is as deliberate as an intent delay could ever prove. `focusin`, which
      // bubbles, so a row whose focusable part is a child is covered too. `:focus-visible`
      // keeps a mouse click out of this path: it focuses the row as well, and opening from
      // here would race the click's own toggle.
      onfocusin: (event: FocusEvent) => {
        const focused = event.target as HTMLElement | null;
        if (focused === null || !focused.matches(":focus-visible")) return;
        cancelHoverIntent();
        openSubmenuFromRow(submenu, event.currentTarget as HTMLElement);
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
    return m(
      "button",
      {
        type: "button",
        role: "menuitem",
        class: menuRowClass({ tightGap: row.tightGap, extra: `${tone} ${row.extraClass ?? ""}`.trim() }),
        "aria-disabled": isDisabled ? "true" : undefined,
        ...rowKeyAttr(row.key),
        ...tooltipAttrs(row.tooltip),
        ...hover,
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          if (isDisabled) return;
          row.onSelect();
          if (row.keepsOpen !== true) close();
        },
      },
      [
        iconBox(row.icon, row.iconBoxClass ?? ICON_BOX_CLASS),
        m("span", { class: ROW_LABEL_CLASS }, row.label),
        row.trailing ?? null,
      ],
    );
  }

  function submenuRow(row: SubmenuRow): m.Vnode {
    const isOpen = openSubmenu === row.key;
    return m(
      "button",
      {
        type: "button",
        role: "menuitem",
        class: menuRowClass({ extra: TONE_CLASS.default }),
        // The row is a disclosure, and a hover menu has to say so out loud: the chevron is the
        // only other clue, and it is decoration to a screen reader.
        "aria-haspopup": "true",
        "aria-expanded": isOpen ? "true" : "false",
        ...rowKeyAttr(row.key),
        ...tooltipAttrs(row.tooltip),
        ...hoverRowAttrs(row),
        // Click still toggles, and is the only way in on a touch screen, where there is no
        // hover to intend anything with.
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
        // The other half of the pair, for the same leave rule: moving between the menu and
        // its submenu is not leaving, but moving off both of them is.
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
    // on arrival AND on every redraw that changes them (a filtered list is shorter, and a
    // triangle pointing at the box's old bottom would guard rows nobody is heading through).
    const place = (vnode: m.VnodeDOM): void => {
      const element = vnode.dom as HTMLElement;
      const box = menuRect ?? element.getBoundingClientRect();
      const placed = placeSubmenu({
        menuLeft: box.left,
        menuWidth: box.width,
        rowTop: submenuRowTop,
        submenuPadding: SUBMENU_PADDING,
        contentHeight: element.getBoundingClientRect().height,
        submenuWidth: element.getBoundingClientRect().width,
        maxSubmenuHeight: maxHeight,
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
        margin: MENU_MARGIN,
        overlap: SUBMENU_OVERLAP,
      });
      element.style.left = `${placed.left}px`;
      element.style.top = `${placed.top}px`;
      element.style.maxHeight = `${placed.maxHeight}px`;
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
        // Arrived. The trip is over, so the wedge that protected it closes and the menu's rows
        // answer the pointer normally again the moment it goes back.
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
      // press from moving focus or starting a selection through the sheet.
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

  function toggle(next: MenuAnchor): void {
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
          toggle((event.currentTarget as HTMLElement).getBoundingClientRect());
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
