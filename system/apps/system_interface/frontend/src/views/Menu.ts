/**
 * The shared floating menu and popover: a card placed against an anchor (hanging under it, or
 * beside it), flipped to the other side when it would overflow the window, rendered into
 * ``<body>`` through the Portal so no window or taskbar clips it, and closed by a press outside
 * it, by Escape, or by a choice. ``placeMenu`` is the one placement rule every floating card
 * uses; the taskbar's popovers hang "below" an anchor at the bottom of the viewport, which is
 * what flips them above it.
 */

import m from "mithril";
import { Portal } from "@imbue/workspace-ui/src/portal";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { IconName } from "@imbue/workspace-ui/src/components/icons";
import { menuCardClass, menuDividerClass, menuRowClass } from "@imbue/workspace-ui/src/components/menu";

export interface MenuAnchor {
  readonly left: number;
  readonly right: number;
  readonly top: number;
  readonly bottom: number;
  readonly width: number;
}

export interface MenuSize {
  readonly width: number;
  readonly height: number;
}

export interface MenuPosition {
  readonly left: number;
  readonly top: number;
}

export type MenuPlacement = "below" | "right";

const MENU_MARGIN = 6;

/**
 * Where a floating card goes: hanging under its anchor ("below") or beside it ("right"). Either
 * way it flips to the opposite side when it would overflow the window and there is room on the
 * other one, then clamps MENU_MARGIN from the edges.
 */
export function placeMenu(
  anchor: MenuAnchor,
  size: MenuSize,
  viewport: MenuSize,
  placement: MenuPlacement,
): MenuPosition {
  let left = placement === "below" ? anchor.left : anchor.right;
  let top = placement === "below" ? anchor.bottom : anchor.top;
  if (placement === "below") {
    const above = anchor.top - size.height;
    if (top + size.height > viewport.height - MENU_MARGIN && above >= MENU_MARGIN) top = above;
  } else {
    const toLeft = anchor.left - size.width;
    if (left + size.width > viewport.width - MENU_MARGIN && toLeft >= MENU_MARGIN) left = toLeft;
  }
  return {
    left: Math.max(Math.min(MENU_MARGIN, anchor.left), Math.min(left, viewport.width - MENU_MARGIN - size.width)),
    top: Math.max(MENU_MARGIN, Math.min(top, viewport.height - MENU_MARGIN - size.height)),
  };
}

/** The anchor of the element an event fired on. */
export function anchorForEvent(event: Event): MenuAnchor {
  return (event.currentTarget as HTMLElement).getBoundingClientRect();
}

/** A zero-width anchor at a pointer position (a context menu). */
export function anchorForPoint(x: number, y: number): MenuAnchor {
  return { left: x, right: x, top: y, bottom: y, width: 0 };
}

/** One actionable row of a menu. */
export interface MenuItem {
  readonly label: string;
  readonly iconName?: IconName;
  readonly isDestructive?: boolean;
  readonly isDisabled?: boolean;
  readonly tooltip?: string;
  /** A bare marker for the tests (``data-menu-item``). */
  readonly key: string;
  readonly run: () => void;
}

export const MENU_DIVIDER = "divider";

export type MenuEntry = MenuItem | typeof MENU_DIVIDER;

export interface FloatingCardAttrs {
  readonly anchor: MenuAnchor;
  readonly placement: MenuPlacement;
  /** ``menu`` for rows of actions, ``dialog`` for a popover with its own content. */
  readonly role: "menu" | "dialog";
  /** A bare marker the tests find the card by (``data-floating``). */
  readonly marker: string;
  readonly onClose: () => void;
  /** Elements whose presses do not close the card (the button that opened it). */
  readonly isInsideTrigger?: (target: Node) => boolean;
}

/**
 * A card on ``<body>`` placed against its anchor. Its children are laid out in viewport
 * coordinates; a press outside it (and outside its trigger) or Escape closes it.
 */
export function FloatingCard(): m.Component<FloatingCardAttrs> {
  let element: HTMLElement | null = null;
  let currentAttrs: FloatingCardAttrs | null = null;

  const onDocumentPointerDown = (event: Event): void => {
    const attrs = currentAttrs;
    if (attrs === null) return;
    const target = event.target;
    if (target instanceof Node) {
      if (element !== null && element.contains(target)) return;
      if (attrs.isInsideTrigger?.(target) === true) return;
    }
    attrs.onClose();
    m.redraw();
  };

  const onDocumentKeyDown = (event: KeyboardEvent): void => {
    if (event.key !== "Escape") return;
    currentAttrs?.onClose();
    m.redraw();
  };

  const place = (vnode: m.VnodeDOM): void => {
    const attrs = currentAttrs;
    if (attrs === null) return;
    const card = vnode.dom as HTMLElement;
    const rect = card.getBoundingClientRect();
    const position = placeMenu(
      attrs.anchor,
      { width: rect.width, height: rect.height },
      { width: window.innerWidth, height: window.innerHeight },
      attrs.placement,
    );
    card.style.left = `${position.left}px`;
    card.style.top = `${position.top}px`;
  };

  return {
    oncreate() {
      document.addEventListener("pointerdown", onDocumentPointerDown, true);
      document.addEventListener("keydown", onDocumentKeyDown);
    },
    onremove() {
      document.removeEventListener("pointerdown", onDocumentPointerDown, true);
      document.removeEventListener("keydown", onDocumentKeyDown);
    },
    view(vnode) {
      currentAttrs = vnode.attrs;
      return m(Portal, {
        children: m(
          "div",
          {
            class: menuCardClass("floating-card fixed min-w-44 text-(length:--font-size-row) text-primary"),
            role: vnode.attrs.role,
            "data-floating": vnode.attrs.marker,
            style: "left: 0; top: 0;",
            oncreate: (created: m.VnodeDOM) => {
              element = created.dom as HTMLElement;
              place(created);
            },
            onupdate: place,
            onremove: () => {
              element = null;
            },
          },
          vnode.children,
        ),
      });
    },
  };
}

export interface MenuAttrs extends Omit<FloatingCardAttrs, "role"> {
  readonly entries: readonly MenuEntry[];
}

/** A floating menu of rows; a row's ``run`` closes the menu first. */
export function Menu(): m.Component<MenuAttrs> {
  return {
    view(vnode) {
      const { entries, onClose, ...card } = vnode.attrs;
      return m(
        FloatingCard,
        { ...card, role: "menu", onClose },
        entries.map((entry, index) => {
          if (entry === MENU_DIVIDER) return m("div", { key: `divider-${index}`, class: menuDividerClass() });
          const tone =
            entry.isDisabled === true
              ? "text-faint cursor-default hover:bg-transparent"
              : entry.isDestructive === true
                ? "text-danger"
                : "text-primary";
          return m(
            "button",
            {
              key: entry.key,
              type: "button",
              role: "menuitem",
              "data-menu-item": entry.key,
              "aria-disabled": entry.isDisabled === true ? "true" : undefined,
              class: `menu-item ${menuRowClass()} ${tone}`,
              ...hoverTooltipAttrs(entry.tooltip ?? null),
              onclick:
                entry.isDisabled === true
                  ? undefined
                  : () => {
                      onClose();
                      entry.run();
                    },
            },
            [
              entry.iconName === undefined
                ? null
                : m(
                    "span",
                    { class: "flex w-4 shrink-0 items-center justify-center" },
                    m.trust(icon(entry.iconName, { size: 14 })),
                  ),
              m("span", { class: "min-w-0 flex-1 truncate" }, entry.label),
            ],
          );
        }),
      );
    },
  };
}
