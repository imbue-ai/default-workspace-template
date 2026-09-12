/**
 * Pure geometry for the workspace's menus: where a menu sits against the thing that opened it
 * (`placeMenu`), where a submenu sits against the row that opened it (`placeSubmenu`), and the
 * wedge a pointer on its way to that submenu is allowed to cross (`isInSafeTriangle`).
 *
 * A MENU hangs off its anchor -- under it, over it, or beside it -- and flips to the opposite
 * side when it would overflow the window and there is room on the other one, then clamps
 * `MENU_MARGIN` from the edges.
 *
 * A SUBMENU's first row lines up with the row that opened it, so the row you pointed at and
 * the list it produced read as one line continuing sideways. When a long list level with a
 * low row would run off the screen, the submenu SLIDES UP by exactly as much as it takes to
 * fit, keeping its height. Only a list too tall for the window at all is capped, and then it
 * scrolls.
 *
 * Kept free of the DOM so it is unit-testable; the caller measures and feeds it in.
 */

/** The part of a ``DOMRect`` the placement needs. A pointer position is one with no width. */
export interface MenuAnchor {
  left: number;
  right: number;
  top: number;
  bottom: number;
  width: number;
}

export interface MenuSize {
  width: number;
  height: number;
}

export interface MenuPosition {
  left: number;
  top: number;
}

/** Which side of its anchor a menu hangs on. */
export type MenuPlacement = "below" | "above" | "right";

/** For `below`/`above`: which of the anchor's vertical edges the menu's own lines up with.
 *  For `right`: which horizontal edge. `start` is the left (or top) edge. */
export type MenuAlign = "start" | "end";

/** Gap kept between a menu and each window edge. */
export const MENU_MARGIN = 6;
/** Gap between a menu and the anchor it hangs off. One value for every menu, so a menu under a
 *  button and a menu beside a row sit the same distance from what opened them. */
export const MENU_GAP = 4;

/**
 * Where a menu goes against its anchor. Prefers the asked-for side; gives way only when that
 * side has no room and the other one does, so a caller's choice holds wherever it can and the
 * fallback is the far side rather than a box half off-screen.
 *
 * The left clamp allows a menu to sit closer than `MENU_MARGIN` to the window's left edge when
 * its ANCHOR is closer still -- the project rail lives at x=0, and its menus should hang off
 * it, not float a margin away from it.
 */
export function placeMenu(
  anchor: MenuAnchor,
  size: MenuSize,
  viewport: MenuSize,
  placement: MenuPlacement,
  align: MenuAlign = "start",
): MenuPosition {
  let left: number;
  let top: number;
  if (placement === "right") {
    const beside = anchor.right + MENU_GAP;
    const otherSide = anchor.left - MENU_GAP - size.width;
    const overflowsRight = beside + size.width > viewport.width - MENU_MARGIN;
    left = overflowsRight && otherSide >= MENU_MARGIN ? otherSide : beside;
    top = align === "start" ? anchor.top : anchor.bottom - size.height;
  } else {
    const under = anchor.bottom + MENU_GAP;
    const over = anchor.top - MENU_GAP - size.height;
    const fitsUnder = under + size.height <= viewport.height - MENU_MARGIN;
    const fitsOver = over >= MENU_MARGIN;
    if (placement === "below") {
      top = !fitsUnder && fitsOver ? over : under;
    } else {
      top = !fitsOver && fitsUnder ? under : over;
    }
    left = align === "start" ? anchor.left : anchor.right - size.width;
  }
  return {
    left: Math.max(Math.min(MENU_MARGIN, anchor.left), Math.min(left, viewport.width - MENU_MARGIN - size.width)),
    top: Math.max(MENU_MARGIN, Math.min(top, viewport.height - MENU_MARGIN - size.height)),
  };
}

export interface SubmenuPlacementInput {
  /** Viewport left of the menu, and its width. */
  menuLeft: number;
  menuWidth: number;
  /** Viewport y of the TOP edge of the row that opened the submenu: what the submenu's first
   *  row lines up with. */
  rowTop: number;
  submenuWidth: number;
  /** The distance from the submenu's outer top edge to the top of its first row -- its border
   *  and its padding -- so `rowTop` aligns the ROW rather than the box that carries it. */
  submenuPadding: number;
  /** How tall the submenu wants to be, from what it is about to hold. Decides whether it has
   *  to slide, and by how much. */
  contentHeight: number;
  /** The tallest the submenu may be before the viewport caps it. */
  maxSubmenuHeight: number;
  viewportWidth: number;
  viewportHeight: number;
  /** Gap kept between the submenu and each viewport edge. */
  margin: number;
  /** How far the submenu tucks under the menu's edge. Positive overlaps. */
  overlap: number;
}

export interface SubmenuPlacement {
  left: number;
  /** Viewport y of the submenu's TOP edge. */
  top: number;
  /** A cap, not a height: the content decides, up to this. */
  maxHeight: number;
  side: "trailing" | "leading";
  /** Whether the alignment had to give way to fit the box on screen. Nothing positions off
   *  this; it is here so a test can say which of the two rules it is exercising. */
  isSlid: boolean;
}

/** A viewport point -- where the pointer is, or where it was. */
export interface MenuPoint {
  x: number;
  y: number;
}

/** The submenu edge a pointer travelling towards it must cross: the side FACING the menu,
 *  and that side's full vertical span. */
export interface SafeTriangleBase {
  edgeX: number;
  top: number;
  bottom: number;
}

/**
 * The safe triangle: is `point` inside the wedge between `apex` and the open submenu's near
 * edge? A pointer travelling diagonally to the submenu crosses the menu's OTHER rows on the
 * way; while it is inside the wedge those rows do not take the hover. The apex is the last
 * point the pointer occupied on the row that opened the submenu, the base is the submenu's
 * near edge: every path between the two stays inside, and a pointer heading anywhere else
 * leaves it almost at once. Pure, like `placeSubmenu`: the caller measures.
 */
export function isInSafeTriangle(point: MenuPoint, apex: MenuPoint, base: SafeTriangleBase): boolean {
  const vertices: readonly MenuPoint[] = [apex, { x: base.edgeX, y: base.top }, { x: base.edgeX, y: base.bottom }];
  // Inside iff `point` sits on the same side of all three edges, walked in order -- so the
  // cross products never disagree in sign. A degenerate triangle (an apex already on the
  // edge, or a submenu of no height) contains only its own line, which reads as "not
  // travelling" and simply leaves the rows unprotected.
  let anyPositive = false;
  let anyNegative = false;
  for (let index = 0; index < vertices.length; index++) {
    const from = vertices[index];
    const to = vertices[(index + 1) % vertices.length];
    const cross = (to.x - from.x) * (point.y - from.y) - (to.y - from.y) * (point.x - from.x);
    if (cross > 0) anyPositive = true;
    if (cross < 0) anyNegative = true;
  }
  return !(anyPositive && anyNegative);
}

export function placeSubmenu(input: SubmenuPlacementInput): SubmenuPlacement {
  const { menuLeft, menuWidth, rowTop, submenuPadding, submenuWidth } = input;
  const { contentHeight, maxSubmenuHeight, viewportWidth, viewportHeight, margin, overlap } = input;

  const trailing = menuLeft + menuWidth - overlap;
  const leading = menuLeft + overlap - submenuWidth;
  // Prefer the trailing side; flip only when the box would not fit there but would fit on the
  // other. A submenu half off-screen is worse than one on the unexpected side.
  const fitsTrailing = trailing + submenuWidth <= viewportWidth - margin;
  const side: "trailing" | "leading" = fitsTrailing || leading < margin ? "trailing" : "leading";
  const wanted = side === "trailing" ? trailing : leading;
  // At absurd viewport widths neither side fits; pin to the left margin so the first
  // characters stay readable.
  const left = Math.min(Math.max(wanted, margin), Math.max(margin, viewportWidth - margin - submenuWidth));

  // The height the box will actually occupy: what it wants, capped by its own row limit and
  // by the window. The slide is measured against THIS rather than against the content, so a
  // list already capped to a scroller does not slide for height it will never use.
  const cap = Math.max(0, Math.min(maxSubmenuHeight, viewportHeight - 2 * margin));
  const height = Math.min(contentHeight, cap);
  // Aligned: the box sits `submenuPadding` above the row, which puts its first row ON the row.
  const aligned = rowTop - submenuPadding;
  // The lowest top that still leaves the whole box on screen. Sliding UP to reach it is what
  // a low row gets instead of a squeezed list.
  const lowestFitting = viewportHeight - margin - height;
  const top = Math.max(margin, Math.min(aligned, lowestFitting));
  return {
    left,
    top,
    maxHeight: cap,
    side,
    isSlid: top !== aligned,
  };
}
