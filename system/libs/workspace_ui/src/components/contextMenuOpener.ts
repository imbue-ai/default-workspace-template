/**
 * The element menu's opener for a Mithril page: the ``open`` that ``installElementContextMenu``
 * takes, backed by the shared Menu so the page's element menu looks like every other menu on
 * the page. The menu portals to <body> through its own render root, so the page's mounted tree
 * needs no slot for it; the owner disposes it when the page goes.
 */

import m from "mithril";

import type { ContextMenuPoint } from "../context_menu";
import type { ContextMenuRow } from "../context_menu_rows";
import { anchorForPoint } from "../menu-position";
import { createMenu, type Menu, type MenuRow } from "./menu";

export interface ContextMenuOpener {
  /** What ``installElementContextMenu`` calls with the rows and the pointer. */
  open(rows: readonly ContextMenuRow[], point: ContextMenuPoint): void;
  /** The menu, for a test or an owner that closes it. */
  readonly menu: Menu;
  /** Close, drop the render root, and free the menu's listeners. */
  dispose(): void;
}

/** A menu of its own with a render root of its own, drawn as ``element-context-menu``. */
export function createContextMenuOpener(ownerDocument: Document = document): ContextMenuOpener {
  const root = ownerDocument.createElement("div");
  root.setAttribute("data-context-menu-root", "");
  ownerDocument.body.appendChild(root);
  let rows: readonly MenuRow[] = [];
  const render = (): void => m.render(root, menu.render(rows));
  const menu = createMenu({
    placement: "below",
    minWidth: 200,
    extraClass: "element-context-menu",
    redraw: render,
  });
  return {
    menu,
    open(nextRows, point) {
      rows = nextRows;
      menu.open(anchorForPoint(point.x, point.y));
    },
    dispose() {
      menu.dispose();
      m.render(root, null);
      root.remove();
    },
  };
}
