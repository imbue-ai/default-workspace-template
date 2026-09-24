/**
 * One shortcut on the backdrop: the app's icon over its label in the cell the grid gave it
 * (``data-shortcut="<app>:<launch>"``, ``data-cell="<column>,<row>"``). A single click selects
 * and a double click, Enter, or Space runs; on touch a tap runs (there is no selection step); a
 * right click or long press asks for the menu. The drag is the gesture layer's, bound by the
 * ``data-shortcut`` attribute, so nothing here listens to pointer movement. While the page has
 * no app list yet, a shortcut whose app it cannot look up draws as connecting
 * (``data-connecting="true"``) rather than as an unknown app.
 */

import m from "mithril";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import type { PixelRect } from "../geometry/frames";
import type { AppRecord, DesktopShortcut, GridCell } from "../model/records";
import { shortcutKey } from "../model/records";
import { appGlyph } from "./glyphs";
import { rectStyle } from "./pixelStyle";

/** The intrinsic width and height the glyph markup carries; the drawing is sized by its token-sized box
 *  (``[&>svg]:size-full``), so the theme's ``--desk-icon-size`` governs, in compact mode too. */
export const ICON_MARKUP_SIZE = 48;

export interface ShortcutIconAttrs {
  readonly shortcut: DesktopShortcut;
  readonly cell: GridCell;
  readonly rect: PixelRect;
  readonly app: AppRecord | undefined;
  /** Whether an app list has landed: an unknown app before that is the page still connecting, not a missing app. */
  readonly isAppsLoaded: boolean;
  readonly isSelected: boolean;
  /** The icon is lifted by a drag: it draws faded in its cell while the ghost follows the pointer. */
  readonly isLifted: boolean;
  /** A click runs the shortcut instead of selecting it (touch: a finger has no double tap worth asking for). */
  readonly isRunOnClick: boolean;
  readonly onSelect: () => void;
  readonly onRun: () => void;
  readonly onContextMenu: (x: number, y: number) => void;
}

/** What a shortcut reads: its app's name, whatever its mode ("Terminal"); the launch path's label is the
 *  launcher tile's. */
export function shortcutLabel(shortcut: DesktopShortcut, app: AppRecord | undefined): string {
  return app === undefined ? shortcut.target.app : app.display_name;
}

export const CONNECTING_TOOLTIP = "Connecting to the workspace...";

/** The icon tile over the label: what a shortcut draws inside its cell, and what its drag ghost carries,
 *  so the thing under the pointer is the thing that was lifted. */
export function shortcutContent(app: AppRecord | undefined, label: string): m.Children {
  return [
    m(
      "span",
      {
        // The hover is the tile growing, not a tint behind it: the tint is what says SELECTED, and
        // one look cannot say both. A transform moves nothing around it.
        class:
          "shortcut-icon relative flex h-(--desk-icon-size) w-(--desk-icon-size) items-center justify-center " +
          "rounded-(--desk-icon-radius) bg-surface p-2 shadow-raised transition-transform " +
          "group-hover:scale-110 [&>svg]:size-full",
      },
      m.trust(appGlyph(app, ICON_MARKUP_SIZE)),
    ),
    // The shadow is the wrapper's filter rather than the text's own: clamping the name to two
    // lines makes its box clip what overflows, and a text-shadow inside that box is cut off at
    // its edges. A filter paints the same silhouette from outside the clip.
    m(
      "span",
      { class: "shortcut-label relative w-full [filter:var(--desk-shortcut-label-shadow)]" },
      m(
        "span",
        {
          class:
            "line-clamp-2 rounded text-(length:--font-size-body) leading-tight font-medium text-on-accent",
        },
        label,
      ),
    ),
  ];
}

/** The hover text a shortcut owes: why it is faint, or nothing when it is ready. */
export function shortcutTooltip(label: string, isStopped: boolean, isConnecting: boolean): string | null {
  if (isConnecting) return CONNECTING_TOOLTIP;
  return isStopped ? `${label}: not running` : null;
}

export function ShortcutIcon(): m.Component<ShortcutIconAttrs> {
  return {
    view(vnode) {
      const {
        shortcut,
        cell,
        rect,
        app,
        isAppsLoaded,
        isSelected,
        isLifted,
        isRunOnClick,
        onSelect,
        onRun,
        onContextMenu,
      } = vnode.attrs;
      const key = shortcutKey(shortcut.target.app, shortcut.target.launch);
      const label = shortcutLabel(shortcut, app);
      const isConnecting = app === undefined && !isAppsLoaded;
      const isStopped = app !== undefined && !app.is_running;
      return m(
        "button",
        {
          type: "button",
          "data-shortcut": key,
          "data-cell": `${cell.column},${cell.row}`,
          "data-connecting": isConnecting ? "true" : null,
          "aria-pressed": isSelected ? "true" : "false",
          class:
            "shortcut group absolute flex flex-col items-center justify-start px-(--desk-cell-gap) " +
            "text-center outline-none touch-none select-none " +
            (isLifted ? "opacity-40 " : "") +
            (isStopped || isConnecting ? "text-faint" : "text-primary"),
          style: rectStyle(rect),
          ...hoverTooltipAttrs(shortcutTooltip(label, isStopped, isConnecting)),
          onclick: isRunOnClick ? onRun : onSelect,
          // A double tap's dblclick follows two clicks that already ran the shortcut.
          ondblclick: (event: MouseEvent) => {
            event.preventDefault();
            if (!isRunOnClick) onRun();
          },
          onkeydown: (event: KeyboardEvent) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              onRun();
            }
          },
          oncontextmenu: (event: MouseEvent) => {
            event.preventDefault();
            onContextMenu(event.clientX, event.clientY);
          },
        },
        // The selection box wraps the icon and the name rather than filling the cell, so it clears
        // each by the same 4px, and it starts at the cell's top rather than centring in it: every
        // icon then sits on one line across the grid, and every name starts on one, whatever wraps
        // to a second line. The room the cell leaves under it is the gap between shortcuts.
        m(
          "span",
          {
            class:
              "shortcut-highlight flex w-full flex-col items-center gap-1 rounded-lg p-1 " +
              "group-focus-visible:outline-2 group-focus-visible:outline-accent " +
              (isSelected ? "bg-accent/15 outline-1 outline-accent" : ""),
          },
          shortcutContent(app, label),
        ),
      );
    },
  };
}
