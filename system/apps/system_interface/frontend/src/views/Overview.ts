/**
 * The overview (the multiplayer spec, section 8.2), opened from the taskbar's overview button, Mission Control
 * style, over the backdrop:
 *
 *   ┌──────────────────────────────────────────────┐
 *   │   [window]   [window]   [window]             │  the active desktop's windows, laid out
 *   │        [window]   [window]                   │  in a grid as live, inert, scaled tiles
 *   │──────────────────────────────────────────────│
 *   │  [Home]  [Research]  [+]      | Left behind  │  the viewer's own desktops as thumbnails,
 *   └──────────────────────────────────────────────┘  then the desktops nobody here owns (the owner alone)
 *
 * A tile is the window's own page, which the live-page layer lays over the tile's content box at the grid's scale
 * (``data-window-id`` and ``data-window-content`` mark the box, as a window's chrome marks its own): clicking one
 * raises the window and closes the overview; a minimized window's tile is drawn at 60%. A thumbnail is drawn from
 * the desktop's wallpaper and this client's placements of it: click to switch (the overview closes), double-click
 * the name (or its menu's Rename row) to rename, drag sideways to reorder, right-click for "Rename" and "Delete
 * desktop...", the deletion confirmed in place over the thumbnail (the row sits against the bottom of the screen).
 * The plus makes a new desktop. Escape or a click on the bare backdrop closes it; both are the App's.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { targetElementOf } from "@imbue/workspace-ui/src/context_menu_rows";
import { frameForState, frameToPixels } from "../geometry/frames";
import type { PixelRect, PixelSize } from "../geometry/frames";
import { OVERVIEW_SCRIM_Z_INDEX } from "../geometry/stacking";
import { wallpaperBackgroundImage } from "../model/api";
import type { AppRecord, Desktop, Layout, WindowRecord } from "../model/records";
import type { ThemeMetrics } from "../theme/metrics";
import { appGlyph } from "./glyphs";

/** The fraction of the overview's height the window grid gets; the desktops row takes the rest. */
export const GRID_SHARE = 0.68;
/** The most a tile scales a window: below full size, so the grid reads as an overview rather than the desktop. */
export const MAX_TILE_SCALE = 0.6;
/** How faint a minimized window's tile is drawn. */
const MINIMIZED_TILE_OPACITY = 0.6;
/** How many thumbnails the desktops row is sized for at least, so a workspace with one desktop draws it no wider
 *  than one of four. */
const THUMBNAIL_ROW_MIN_COUNT = 4;
/** The glyph sizes of a tile's title strip and its caption, and of a thumbnail's mini windows. */
const TILE_GLYPH_SIZE = 20;
const CAPTION_GLYPH_SIZE = 14;
const MINI_WINDOW_GLYPH_SIZE = 6;
const PLUS_GLYPH_SIZE = 22;
/** The fraction of a thumbnail's width the plus takes. */
const PLUS_WIDTH_SHARE = 0.6;

/** One window of the active desktop as the overview tiles it. */
export interface OverviewTile {
  readonly window: WindowRecord;
  readonly app: AppRecord | undefined;
  readonly title: string;
  /** The rectangle the window renders at on the desktop, which the tile shows scaled down. */
  readonly rect: PixelRect;
  readonly isMinimized: boolean;
}

/** How the tiles are laid out: as many columns as keep the tiles largest, and the one scale they all share. */
export interface OverviewGrid {
  readonly columns: number;
  readonly scale: number;
}

/** The grid for ``rects`` in a ``width`` by ``height`` area: for every column count, the scale every window can
 *  share in that count's cell (capped at ``MAX_TILE_SCALE``); the count with the largest scale wins. */
export function gridFor(
  rects: readonly PixelRect[],
  width: number,
  height: number,
  metrics: ThemeMetrics,
): OverviewGrid {
  const count = rects.length;
  if (count === 0) return { columns: 1, scale: MAX_TILE_SCALE };
  const gap = metrics.overviewGap;
  let best: OverviewGrid = { columns: 1, scale: 0 };
  for (let columns = 1; columns <= count; columns += 1) {
    const rows = Math.ceil(count / columns);
    const cellWidth = (width - gap * (columns + 1)) / columns;
    const cellHeight = (height - gap * (rows + 1)) / rows - metrics.overviewTileTitle;
    const scale = Math.min(
      MAX_TILE_SCALE,
      ...rects.map((rect) => Math.min(cellWidth / Math.max(rect.width, 1), cellHeight / Math.max(rect.height, 1))),
    );
    if (scale > best.scale) best = { columns, scale };
  }
  return best;
}

/** The size the desktops row draws each thumbnail at: as wide as the token allows, narrower when the row would
 *  not fit that many (the viewer's own and the ones left behind alike), and in the backdrop's proportion. */
export function thumbnailSize(backdrop: PixelSize, desktopCount: number, metrics: ThemeMetrics): PixelSize {
  const gap = metrics.overviewGap;
  const fitted = (backdrop.width - gap * 2) / Math.max(THUMBNAIL_ROW_MIN_COUNT, desktopCount + 1) - gap / 2;
  const width = Math.max(1, Math.min(metrics.overviewThumbnailWidth, fitted));
  const height = backdrop.width > 0 ? (width * backdrop.height) / backdrop.width : width;
  return { width, height };
}

/** A gap of the desktops row (0 to n) a dragged thumbnail would land in. */
export interface ThumbnailDrag {
  readonly desktopId: string;
  readonly gap: number;
}

/** The desktops in their new order once ``drag`` lands: the lifted one out, then back in at its gap. */
export function reorderedForDrop(desktops: readonly Desktop[], drag: ThumbnailDrag): string[] {
  const from = desktops.findIndex((desktop) => desktop.id === drag.desktopId);
  if (from < 0) return desktops.map((desktop) => desktop.id);
  const rest = desktops.filter((desktop) => desktop.id !== drag.desktopId).map((desktop) => desktop.id);
  // The gap counts the lifted desktop; its slot among the rest does not.
  const at = Math.max(0, Math.min(drag.gap > from ? drag.gap - 1 : drag.gap, rest.length));
  return [...rest.slice(0, at), drag.desktopId, ...rest.slice(at)];
}

export interface OverviewAttrs {
  /** The active desktop, whose windows are tiled. */
  readonly desktop: Desktop;
  /** Its windows, front-most first. */
  readonly tiles: readonly OverviewTile[];
  readonly grid: OverviewGrid;
  readonly backdrop: PixelSize;
  readonly metrics: ThemeMetrics;
  /** The viewer's own desktops, in the shell's order. */
  readonly ownDesktops: readonly Desktop[];
  /** The desktops nobody in the roster owns, which the owner alone is shown. */
  readonly leftBehind: readonly Desktop[];
  readonly apps: readonly AppRecord[];
  /** This client's layout of a desktop, for its thumbnail; null while it is still being read. */
  readonly layoutOf: (desktopId: string) => Layout | null;
  /** The desktop whose deletion is being confirmed in place, if any. */
  readonly confirmingDeleteId: string | null;
  /** The desktop whose name is being edited in place, if any. */
  readonly renamingId: string | null;
  readonly onRaiseWindow: (windowId: string) => void;
  readonly onSwitchDesktop: (desktopId: string) => void;
  /** Start editing the desktop's name (a double-click, or the thumbnail menu's Rename row). */
  readonly onBeginRename: (desktopId: string) => void;
  /** The name edit is over, committed or abandoned. */
  readonly onEndRename: () => void;
  readonly onRename: (desktopId: string, name: string) => void;
  /** The viewer's own desktops in their new order (every one of them). */
  readonly onReorder: (desktopIds: readonly string[]) => void;
  readonly onDesktopContextMenu: (desktopId: string, x: number, y: number, target: Element) => void;
  readonly onConfirmDelete: (desktopId: string) => void;
  readonly onKeepDesktop: () => void;
  readonly onNewDesktop: () => void;
  readonly onClose: () => void;
}

/** Closes the overview on a click that lands on the element itself rather than on something in it. */
function closeOnBareClick(onClose: () => void): (event: MouseEvent) => void {
  return (event) => {
    if (event.target === event.currentTarget) onClose();
  };
}

export function Overview(): m.Component<OverviewAttrs> {
  /** What the rename input holds while an edit is on: kept here, not as the input's attribute, so the constant
   *  redraws around the overview (presence pushes, live tiles) never reset what was typed. */
  let renameDraft: { desktopId: string; text: string } | null = null;
  /** A thumbnail being dragged to a new place, and the gap it would land in. */
  let drag: ThumbnailDrag | null = null;

  function commitRename(attrs: OverviewAttrs, desktopId: string): void {
    if (attrs.renamingId !== desktopId) return;
    const name = (renameDraft?.desktopId === desktopId ? renameDraft.text : "").trim();
    renameDraft = null;
    attrs.onEndRename();
    const current = attrs.ownDesktops.find((desktop) => desktop.id === desktopId);
    if (name !== "" && current !== undefined && name !== current.name) attrs.onRename(desktopId, name);
  }

  function windowTile(attrs: OverviewAttrs, tile: OverviewTile): m.Vnode {
    const { grid, metrics } = attrs;
    const width = tile.rect.width * grid.scale;
    const height = tile.rect.height * grid.scale;
    const raise = (): void => {
      attrs.onRaiseWindow(tile.window.id);
      attrs.onClose();
    };
    return m(
      "div",
      {
        key: tile.window.id,
        // Not a <button>: the tile holds the window's live page, which has buttons of its own.
        role: "button",
        tabindex: 0,
        "data-overview-window": tile.window.id,
        "data-minimized": tile.isMinimized ? "true" : "false",
        "aria-label": `Show ${tile.title}`,
        class: "overview-tile group flex cursor-pointer flex-col items-center gap-1.5",
        style: { flexBasis: `calc(${100 / grid.columns}% - ${metrics.overviewGap}px)`, maxWidth: `${width}px` },
        onclick: raise,
        onkeydown: (event: KeyboardEvent) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            raise();
          }
        },
      },
      [
        m(
          "div",
          {
            // The window's chrome as the live-page layer knows it: the page is laid over the content box below.
            "data-window-id": tile.window.id,
            class:
              "overview-tile-frame relative flex flex-col overflow-hidden rounded-lg bg-surface shadow-overlay " +
              "ring-white/0 transition-[box-shadow,transform] duration-(--dur-fast) " +
              "group-hover:scale-[1.02] group-hover:ring-4 group-hover:ring-white/70",
            style: { width: `${width}px`, opacity: tile.isMinimized ? String(MINIMIZED_TILE_OPACITY) : "" },
          },
          [
            m(
              "div",
              {
                class:
                  "flex shrink-0 items-center gap-2 border-b border-default px-2 text-(length:--font-size-row) " +
                  "font-semibold text-primary",
                style: { height: `${metrics.overviewTileTitle}px` },
              },
              [
                m(
                  "span",
                  { class: "flex shrink-0 items-center [&>svg]:size-5" },
                  m.trust(appGlyph(tile.app, TILE_GLYPH_SIZE)),
                ),
                m("span", { class: "truncate" }, tile.title),
              ],
            ),
            m("div", { "data-window-content": "", class: "relative", style: { height: `${height}px` } }),
          ],
        ),
        m(
          "span",
          {
            class:
              "flex max-w-full items-center gap-1.5 rounded-full bg-black/30 px-2.5 py-0.5 text-(length:--font-size-helper) text-white",
          },
          [
            m(
              "span",
              { class: "flex shrink-0 items-center [&>svg]:size-3.5" },
              m.trust(appGlyph(tile.app, CAPTION_GLYPH_SIZE)),
            ),
            m("span", { class: "truncate" }, tile.title),
            tile.isMinimized ? m("span", { class: "text-white/60" }, "· minimized") : null,
          ],
        ),
      ],
    );
  }

  function miniWindows(attrs: OverviewAttrs, desktop: Desktop, size: PixelSize): m.Children {
    const layout = attrs.layoutOf(desktop.id);
    if (layout === null) return null;
    return layout.placements.flatMap((placement) => {
      const window = desktop.windows.find((candidate) => candidate.id === placement.window_id);
      if (window === undefined || placement.is_minimized || placement.is_detached) return [];
      const rect = frameToPixels(frameForState(placement.frame, placement.state), size);
      const app = attrs.apps.find((candidate) => candidate.name === window.app);
      return [
        m(
          "div",
          {
            key: window.id,
            "data-overview-mini-window": window.id,
            class: "absolute flex flex-col overflow-hidden rounded-xs bg-white/95 shadow-raised",
            style: {
              left: `${rect.x}px`,
              top: `${rect.y}px`,
              width: `${rect.width}px`,
              height: `${rect.height}px`,
            },
          },
          m(
            "div",
            { class: "flex h-2 shrink-0 items-center border-b border-black/10 px-0.5 [&>svg]:size-1.5" },
            m.trust(appGlyph(app, MINI_WINDOW_GLYPH_SIZE)),
          ),
        ),
      ];
    });
  }

  /** The gap a pointer at ``clientX`` over the thumbnail at ``index`` would drop into: before it or after it. */
  function gapAt(element: HTMLElement, index: number, clientX: number): number {
    const box = element.getBoundingClientRect();
    return clientX < box.left + box.width / 2 ? index : index + 1;
  }

  function thumbnail(
    attrs: OverviewAttrs,
    desktop: Desktop,
    index: number,
    size: PixelSize,
    isReorderable: boolean,
  ): m.Vnode {
    const isActive = desktop.id === attrs.desktop.id;
    const isRenaming = attrs.renamingId === desktop.id;
    if (isRenaming && renameDraft?.desktopId !== desktop.id) renameDraft = { desktopId: desktop.id, text: desktop.name };
    const isConfirmingDelete = attrs.confirmingDeleteId === desktop.id;
    const landing =
      isReorderable &&
      drag !== null &&
      drag.desktopId !== desktop.id &&
      (drag.gap === index || drag.gap === index + 1);
    return m(
      "div",
      {
        key: desktop.id,
        "data-overview-desktop-slot": desktop.id,
        class: "relative flex flex-col items-center gap-2",
        draggable: isReorderable && !isRenaming ? "true" : undefined,
        ondragstart: (event: DragEvent) => {
          if (!isReorderable) return;
          // A browser's drag carries a transfer; a synthetic event may carry none.
          const transfer = event.dataTransfer ?? null;
          if (transfer !== null) {
            transfer.setData("text/plain", desktop.id);
            transfer.effectAllowed = "move";
          }
          drag = { desktopId: desktop.id, gap: index };
        },
        ondragover: (event: DragEvent) => {
          if (drag === null || !isReorderable) return;
          event.preventDefault();
          const gap = gapAt(event.currentTarget as HTMLElement, index, event.clientX);
          if (gap !== drag.gap) drag = { ...drag, gap };
        },
        ondrop: (event: DragEvent) => {
          event.preventDefault();
          if (drag === null || !isReorderable) return;
          const ordered = reorderedForDrop(attrs.ownDesktops, drag);
          drag = null;
          if (ordered.some((desktopId, position) => attrs.ownDesktops[position]?.id !== desktopId)) {
            attrs.onReorder(ordered);
          }
        },
        ondragend: () => {
          drag = null;
        },
      },
      [
        // Where it will land: a bar in the gap before or after this desktop.
        landing && drag !== null
          ? m("span", {
              "data-overview-drop-gap": String(drag.gap),
              class:
                "pointer-events-none absolute top-0 w-0.5 rounded-full bg-white " +
                (drag.gap === index ? "-left-2.5" : "-right-2.5"),
              style: { height: `${size.height}px` },
            })
          : null,
        m(
          "button",
          {
            type: "button",
            "data-overview-desktop": desktop.id,
            "data-active": isActive ? "true" : "false",
            "aria-current": isActive ? "true" : undefined,
            "aria-label": `Switch to ${desktop.name}`,
            class:
              "overview-thumbnail group relative cursor-pointer overflow-hidden rounded-(--desk-overview-thumbnail-radius) " +
              "border-0 bg-cover bg-center bg-(image:--desk-default-wallpaper) p-0 shadow-raised outline-offset-2 " +
              "transition-[transform,opacity] duration-(--dur-fast) hover:scale-[1.04] " +
              (isActive ? "outline-3 outline-white " : "outline-0 ") +
              (drag?.desktopId === desktop.id ? "opacity-40" : ""),
            style: {
              width: `${size.width}px`,
              height: `${size.height}px`,
              ...(desktop.wallpaper === null ? {} : { backgroundImage: wallpaperBackgroundImage(desktop.wallpaper) }),
            },
            onclick: () => attrs.onSwitchDesktop(desktop.id),
            oncontextmenu: (event: MouseEvent) => {
              event.preventDefault();
              attrs.onDesktopContextMenu(desktop.id, event.clientX, event.clientY, targetElementOf(event));
            },
          },
          miniWindows(attrs, desktop, size),
        ),
        isRenaming
          ? m("input", {
              "data-overview-rename": desktop.id,
              "aria-label": "Desktop name",
              type: "text",
              value: renameDraft?.text ?? desktop.name,
              class:
                "w-36 rounded-md border-0 bg-white/90 px-1.5 py-0.5 text-center text-(length:--font-size-helper) " +
                "text-black outline-none",
              oncreate: (created: m.VnodeDOM) => {
                const input = created.dom as HTMLInputElement;
                input.focus();
                input.select();
              },
              oninput: (event: InputEvent) => {
                renameDraft = { desktopId: desktop.id, text: (event.target as HTMLInputElement).value };
              },
              onblur: () => commitRename(attrs, desktop.id),
              onkeydown: (event: KeyboardEvent) => {
                // The overview's own Escape and the document's stay out of a rename in progress.
                event.stopPropagation();
                if (event.key === "Enter") (event.target as HTMLInputElement).blur();
                if (event.key === "Escape") {
                  // Ending the edit first makes the blur that follows commit nothing.
                  renameDraft = null;
                  attrs.onEndRename();
                }
              },
            })
          : m(
              "button",
              {
                type: "button",
                "data-overview-desktop-name": desktop.id,
                class:
                  "max-w-40 truncate rounded-md border-0 bg-transparent px-1.5 py-0.5 text-(length:--font-size-helper) " +
                  "hover:bg-white/10 " +
                  (isReorderable ? "cursor-text " : "cursor-default ") +
                  (isActive ? "font-semibold text-white" : "text-white/80"),
                ...hoverTooltipAttrs(isReorderable ? "Double-click to rename" : null, "above"),
                ondblclick: () => {
                  if (isReorderable) attrs.onBeginRename(desktop.id);
                },
              },
              desktop.name,
            ),
        isConfirmingDelete
          ? m(
              "div",
              {
                "data-overview-delete-confirmation": desktop.id,
                // Above the thumbnail: the desktops row sits against the bottom of the screen, so a confirmation
                // hung below it would clip past the viewport, buttons first.
                class:
                  "absolute bottom-full left-1/2 z-(--z-popover) mb-1 flex w-56 -translate-x-1/2 flex-col gap-2 rounded-xl " +
                  "border border-default bg-surface p-3 text-(length:--font-size-row) text-primary shadow-overlay",
              },
              [
                m("div", ["Delete ", m("strong", desktop.name), "? Its windows close for everyone."]),
                m("div", { class: "flex justify-end gap-2" }, [
                  m(Button, { sm: true, "data-overview-keep": "", onclick: attrs.onKeepDesktop }, "Keep"),
                  m(
                    Button,
                    {
                      variant: "destructive",
                      sm: true,
                      "data-overview-confirm-delete": desktop.id,
                      onclick: () => attrs.onConfirmDelete(desktop.id),
                    },
                    "Delete desktop",
                  ),
                ]),
              ],
            )
          : null,
      ],
    );
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const { backdrop, metrics } = attrs;
      const gridHeight = backdrop.height * GRID_SHARE;
      const size = thumbnailSize(backdrop, attrs.ownDesktops.length + attrs.leftBehind.length, metrics);
      const close = closeOnBareClick(attrs.onClose);
      return m(
        "div",
        {
          "data-overview": "",
          role: "dialog",
          "aria-label": "Desktop overview",
          class: "overview absolute inset-0 flex flex-col bg-black/35 backdrop-blur-md",
          style: { zIndex: OVERVIEW_SCRIM_Z_INDEX },
          onclick: close,
        },
        [
          m(
            "div",
            {
              "data-overview-windows": "",
              role: "presentation",
              class: "flex min-h-0 flex-wrap content-center items-center justify-center",
              style: {
                height: `${gridHeight}px`,
                gap: `${metrics.overviewGap}px`,
                padding: `${metrics.overviewGap}px`,
              },
              onclick: close,
            },
            attrs.tiles.length === 0
              ? m(
                  "span",
                  { "data-overview-no-windows": "", class: "text-(length:--font-size-body) text-white/80" },
                  `No windows open on ${attrs.desktop.name}`,
                )
              : attrs.tiles.map((tile) => windowTile(attrs, tile)),
          ),
          m(
            "div",
            {
              "data-overview-desktops": "",
              role: "presentation",
              class: "flex min-h-0 flex-1 items-center justify-center gap-4 border-t border-white/15 bg-black/20 px-6",
              onclick: close,
            },
            [
              attrs.ownDesktops.map((desktop, index) => thumbnail(attrs, desktop, index, size, true)),
              m(
                "button",
                {
                  type: "button",
                  "data-overview-new-desktop": "",
                  "aria-label": "New desktop",
                  class: "flex cursor-pointer flex-col items-center gap-2 border-0 bg-transparent p-0",
                  onclick: attrs.onNewDesktop,
                },
                [
                  m(
                    "span",
                    {
                      class: "flex items-center justify-center text-white/80 hover:text-white",
                      style: { width: `${size.width * PLUS_WIDTH_SHARE}px`, height: `${size.height}px` },
                    },
                    m.trust(icon("plus", { size: PLUS_GLYPH_SIZE })),
                  ),
                  // No label, but a label's height, so the plus sits level with the thumbnails.
                  m(
                    "span",
                    { "aria-hidden": "true", class: "invisible py-0.5 text-(length:--font-size-helper)" },
                    "New",
                  ),
                ],
              ),
              attrs.leftBehind.length === 0
                ? null
                : m(
                    "div",
                    {
                      "data-overview-left-behind": "",
                      class: "ml-2 flex items-center gap-4 border-l border-white/25 pl-6",
                    },
                    [
                      m(
                        "span",
                        {
                          class:
                            "shrink-0 text-(length:--font-size-helper) tracking-wide text-white/60 uppercase [writing-mode:vertical-rl] rotate-180",
                        },
                        "Left behind",
                      ),
                      attrs.leftBehind.map((desktop, index) => thumbnail(attrs, desktop, index, size, false)),
                    ],
                  ),
            ],
          ),
        ],
      );
    },
  };
}
