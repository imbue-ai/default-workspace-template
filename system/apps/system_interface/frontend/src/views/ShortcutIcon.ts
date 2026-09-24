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
import { targetElementOf } from "@imbue/workspace-ui/src/context_menu_rows";
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
  readonly onContextMenu: (x: number, y: number, target: Element) => void;
}

/** What a shortcut reads: its app's name, whatever its mode ("Terminal"); the launch path's label is the
 *  launcher tile's. */
export function shortcutLabel(shortcut: DesktopShortcut, app: AppRecord | undefined): string {
  return app === undefined ? shortcut.target.app : app.display_name;
}

export const CONNECTING_TOOLTIP = "Connecting to the workspace...";

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
            "shortcut absolute flex flex-col items-center gap-1 rounded-lg p-1 text-center outline-none " +
            "focus-visible:ring-2 focus-visible:ring-accent touch-none select-none " +
            (isSelected ? "bg-fill-active " : "hover:bg-fill-hover ") +
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
            onContextMenu(event.clientX, event.clientY, targetElementOf(event));
          },
        },
        [
          m(
            "span",
            {
              class:
                "shortcut-icon flex h-(--desk-icon-size) w-(--desk-icon-size) items-center justify-center rounded-xl " +
                "bg-surface shadow-raised [&>svg]:size-full",
            },
            m.trust(appGlyph(app, ICON_MARKUP_SIZE)),
          ),
          m(
            "span",
            {
              class:
                "shortcut-label line-clamp-2 w-full rounded px-1 text-(length:--font-size-helper) leading-tight " +
                "text-on-accent [text-shadow:var(--desk-shortcut-label-shadow)]",
            },
            label,
          ),
        ],
      );
    },
  };
}
