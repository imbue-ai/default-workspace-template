/**
 * One shortcut on the backdrop: the app's icon over its label in the cell the grid gave it
 * (``data-shortcut="<app>:<launch>"``, ``data-cell="<column>,<row>"``). A single click selects
 * and a double click, Enter, or Space runs; on touch a tap runs (there is no selection step); a
 * right click or long press asks for the menu. The drag is the gesture layer's, bound by the
 * ``data-shortcut`` attribute, so nothing here listens to pointer movement; the one being dragged is
 * the icon ITSELF, translated out of its own cell (``data-lifted``) rather than copied into a ghost, and
 * whichever shortcut it is held over slides aside -- for the length of that drag only -- so the one in the
 * hand has somewhere to land. While the page has
 * no app list yet, a shortcut whose app it cannot look up draws as connecting
 * (``data-connecting="true"``) rather than as an unknown app.
 */

import m from "mithril";
import { targetElementOf } from "@imbue/workspace-ui/src/context_menu_rows";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import type { PixelPoint, PixelRect } from "../geometry/frames";
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
  /** How far a drag has carried this icon out of its cell, or null when it is resting: the icon in the hand
   *  is drawn in the cell it was lifted from and translated by this, at full strength and above everything. */
  readonly lift: PixelPoint | null;
  /** Whether a drag is making room right now: only then does an icon slide between cells. Outside a drag a
   *  cell changes because the grid was re-fitted -- a desktop switch, or a resize, which re-fits it on every
   *  frame of the drag. */
  readonly isSliding: boolean;
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

/** The three properties a lift writes, spelled once for the render and the paint. */
export interface LiftStyle {
  readonly transform: string;
  readonly transition: string;
  readonly zIndex: string;
}

/** The icon tile over the label: what a shortcut draws inside its cell. The whole thing is what a drag
 *  carries, so the tile forgoes its own hover growth while lifted rather than compounding it with the lift. */
function shortcutContent(app: AppRecord | undefined, label: string, isLifted: boolean): m.Children {
  return [
    m(
      "span",
      {
        // The hover is the tile growing: a tint behind it is what says selected, and one look
        // cannot say both. A transform moves nothing around it.
        class:
          "shortcut-icon relative flex h-(--desk-icon-size) w-(--desk-icon-size) items-center justify-center " +
          "rounded-(--desk-icon-radius) bg-surface p-2 [&>svg]:size-full " +
          (isLifted
            ? "shadow-(--desk-icon-shadow-lifted)"
            : "shadow-(--desk-icon-shadow) transition-transform group-hover:scale-110"),
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
          class: "line-clamp-2 rounded text-(length:--font-size-body) leading-tight font-bold text-on-accent",
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

/** What being in the hand comes to, spelled once for the render and for the per-frame paint of the drag:
 *  the icon translated out of its cell and grown, over everything, with nothing eased so it tracks the
 *  pointer exactly. Resting (``null``) clears all three. */
export function liftStyle(lift: PixelPoint | null): LiftStyle {
  if (lift === null) return { transform: "", transition: "", zIndex: "" };
  return {
    transform: `translate(${lift.x}px, ${lift.y}px) scale(var(--desk-shortcut-lift-scale))`,
    transition: "none",
    zIndex: "var(--z-sticky)",
  };
}

/** Write the lift onto an icon outside any redraw. */
export function applyLiftStyle(element: HTMLElement, lift: PixelPoint | null): void {
  Object.assign(element.style, liftStyle(lift));
}

/** Write a dropped icon's landing straight onto it: the cell it landed in, the lift cleared, and the slide
 *  off for this one frame -- the hand left it at that cell, so sliding it over from the cell it was picked up
 *  in would take it backwards. The next render writes the same box and lets the slide back on. */
export function applyDropStyle(element: HTMLElement, rect: PixelRect): void {
  Object.assign(element.style, rectStyle(rect), { transform: "", transition: "none", zIndex: "" });
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
        lift,
        isSliding,
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
          "data-lifted": lift === null ? null : "true",
          "aria-pressed": isSelected ? "true" : "false",
          class:
            "shortcut group absolute flex cursor-grab flex-col items-center justify-start " +
            "px-(--desk-cell-gap) text-center outline-none touch-none select-none " +
            // An icon that is not in the hand slides between cells, which is how the desktop shows where the
            // one in the hand would land. The one in the hand is not transitioned at all: it tracks the pointer.
            (isSliding && lift === null ? "transition-[left,top] duration-(--desk-shortcut-slide) ease-out " : "") +
            (isStopped || isConnecting ? "text-faint" : "text-primary"),
          style: { ...rectStyle(rect), ...liftStyle(lift) },
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
        // The box wraps the icon and the name, and starts at the cell's top: every icon then sits
        // on one line across the grid and every name starts on one, whatever wraps to a second
        // line. What the cell leaves under it is the gap to the shortcut below.
        m(
          "span",
          {
            class:
              "shortcut-highlight flex w-full flex-col items-center gap-3 rounded-lg p-1 " +
              "group-focus-visible:outline-2 group-focus-visible:outline-accent " +
              (isSelected ? "bg-accent/15 outline-1 outline-accent" : ""),
          },
          shortcutContent(app, label, lift !== null),
        ),
      );
    },
  };
}
