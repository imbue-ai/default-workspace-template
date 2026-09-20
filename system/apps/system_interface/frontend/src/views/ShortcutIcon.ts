/**
 * One shortcut on the backdrop: the app's icon over its label in the cell the grid gave it
 * (``data-shortcut="<app>:<launch>"``, ``data-cell="<column>,<row>"``). A single click or tap
 * selects, a double click, Enter, or Space runs, a right click or long press asks for the menu;
 * the drag is the gesture layer's, bound by the ``data-shortcut`` attribute, so nothing here
 * listens to pointer movement.
 */

import m from "mithril";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import type { PixelRect } from "../geometry/frames";
import { launchPathOf } from "../model/launch";
import type { AppRecord, DesktopShortcut, GridCell } from "../model/records";
import { shortcutKey } from "../model/records";
import { appGlyph } from "./glyphs";

/** The intrinsic width and height the glyph markup carries; the drawing is sized by its token-sized box
 *  (``[&>svg]:size-full``), so the theme's ``--desk-icon-size`` governs, in compact mode too. */
export const ICON_MARKUP_SIZE = 48;

export interface ShortcutIconAttrs {
  readonly shortcut: DesktopShortcut;
  readonly cell: GridCell;
  readonly rect: PixelRect;
  readonly app: AppRecord | undefined;
  readonly isSelected: boolean;
  /** The icon is lifted by a drag: it draws faded in its cell while the ghost follows the pointer. */
  readonly isLifted: boolean;
  readonly onSelect: () => void;
  readonly onRun: () => void;
  readonly onContextMenu: (x: number, y: number) => void;
}

/** What a shortcut reads: the launch path's label while it always creates ("New Terminal"), the
 *  app's name while it focuses ("Terminal"). */
export function shortcutLabel(shortcut: DesktopShortcut, app: AppRecord | undefined): string {
  if (app === undefined) return shortcut.target.app;
  const launchPath = launchPathOf(app, shortcut.target.launch);
  return shortcut.mode === "new" && launchPath !== null ? launchPath.label : app.display_name;
}

export function ShortcutIcon(): m.Component<ShortcutIconAttrs> {
  return {
    view(vnode) {
      const { shortcut, cell, rect, app, isSelected, isLifted, onSelect, onRun, onContextMenu } = vnode.attrs;
      const key = shortcutKey(shortcut.target.app, shortcut.target.launch);
      const label = shortcutLabel(shortcut, app);
      const isStopped = app !== undefined && !app.is_running;
      return m(
        "button",
        {
          type: "button",
          "data-shortcut": key,
          "data-cell": `${cell.column},${cell.row}`,
          "aria-pressed": isSelected ? "true" : "false",
          class:
            "shortcut absolute flex flex-col items-center gap-1 rounded-lg p-1 text-center outline-none " +
            "focus-visible:ring-2 focus-visible:ring-accent touch-none select-none " +
            (isSelected ? "bg-fill-active " : "hover:bg-fill-hover ") +
            (isLifted ? "opacity-40 " : "") +
            (isStopped ? "text-faint" : "text-primary"),
          style: {
            left: `${rect.x}px`,
            top: `${rect.y}px`,
            width: `${rect.width}px`,
            height: `${rect.height}px`,
          },
          ...hoverTooltipAttrs(isStopped ? `${label}: not running` : null),
          onclick: onSelect,
          ondblclick: (event: MouseEvent) => {
            event.preventDefault();
            onRun();
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
                "text-on-accent [text-shadow:0_1px_2px_rgb(0_0_0/0.6)]",
            },
            label,
          ),
        ],
      );
    },
  };
}
