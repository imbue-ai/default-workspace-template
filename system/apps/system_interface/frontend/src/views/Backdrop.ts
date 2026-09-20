/**
 * The backdrop (concepts.md section 2.4): the active desktop's wallpaper, its shortcut grid
 * fitted to the current backdrop at render time, its windows in stacking order (each over the
 * live page the pages layer positions for it), the floating pinned entries above them, the snap
 * preview, and the ghost of a dragged shortcut. Every pixel comes from the store's geometry;
 * nothing here measures the DOM or listens for gestures.
 */

import m from "mithril";
import { wallpaperImageUrl } from "../model/api";
import { cellRect, placeShortcuts } from "../geometry/grid";
import type { PixelPoint, PixelRect } from "../geometry/frames";
import type { AppRecord, Desktop, DesktopShortcut, Placement } from "../model/records";
import { shortcutKey } from "../model/records";
import { appByName, effectiveWindowTitle, renderedState } from "../reducers/desktopState";
import type { TaskbarEntry } from "../reducers/desktopState";
import type { DesktopStore } from "../store/DesktopStore";
import { FloatingEntries } from "./FloatingEntries";
import { ICON_MARKUP_SIZE, ShortcutIcon } from "./ShortcutIcon";
import { SnapPreview } from "./SnapPreview";
import { Window } from "./Window";
import type { WindowControl } from "./TitleBar";
import { appGlyph } from "./glyphs";

export interface BackdropAttrs {
  readonly store: DesktopStore;
  readonly desktop: Desktop;
  readonly placements: readonly Placement[];
  readonly focusedWindowId: string | null;
  readonly selectedShortcutKey: string | null;
  readonly openMenuWindowId: string | null;
  /** The pinned entries this client draws floating above the windows. */
  readonly floatingEntries: readonly TaskbarEntry[];
  readonly openEntryMenuWindowId: string | null;
  readonly onEntryClick: (windowId: string) => void;
  readonly onEntryContextMenu: (windowId: string, x: number, y: number) => void;
  /** Whether a menu or the launcher is open: every window is shielded, so the press that closes it reaches the shell. */
  readonly isOverlayOpen: boolean;
  readonly onSelectShortcut: (key: string | null) => void;
  readonly onRunShortcut: (shortcut: DesktopShortcut) => void;
  readonly onShortcutContextMenu: (shortcut: DesktopShortcut, point: PixelPoint) => void;
  readonly onWindowControl: (windowId: string, control: WindowControl, event: MouseEvent) => void;
  /** The element the live pages are appended to, created once and never re-rendered. */
  readonly onPagesHostCreated: (host: HTMLElement) => void;
}

export function Backdrop(): m.Component<BackdropAttrs> {
  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const { store, desktop, placements, focusedWindowId } = attrs;
      const state = store.getState();
      const metrics = store.getMetrics();
      const gesture = store.getGesture();
      const dimensions = store.gridDimensions();
      const placed = placeShortcuts(desktop.shortcuts, dimensions);
      const liftedKey = gesture?.kind === "shortcut" ? shortcutKey(gesture.app, gesture.launch) : null;
      const windowsById = new Map(desktop.windows.map((window) => [window.id, window]));
      const snapRect = store.snapPreviewRect();
      const wallpaperStyle =
        desktop.wallpaper === null ? {} : { backgroundImage: `url("${wallpaperImageUrl(desktop.wallpaper)}")` };

      return m(
        "div",
        {
          "data-desktop-id": desktop.id,
          class:
            "backdrop relative isolate h-full w-full overflow-hidden select-none bg-(--desk-backdrop) bg-cover " +
            "bg-center bg-(image:--desk-default-wallpaper)",
          style: wallpaperStyle,
        },
        [
          m(
            "div",
            {
              class: "shortcut-grid absolute inset-0",
              onpointerdown: (event: PointerEvent) => {
                // A press on the bare backdrop (the grid covers all of it) clears the shortcut selection.
                if (event.target === event.currentTarget) attrs.onSelectShortcut(null);
              },
            },
            placed.map(({ shortcut, cell }) => {
              const key = shortcutKey(shortcut.target.app, shortcut.target.launch);
              return m(ShortcutIcon, {
                key,
                shortcut,
                cell,
                rect: cellRect(cell, metrics),
                app: appByName(state, shortcut.target.app),
                isSelected: attrs.selectedShortcutKey === key,
                isLifted: liftedKey === key,
                isRunOnClick: state.modes.isTouch,
                onSelect: () => attrs.onSelectShortcut(key),
                onRun: () => attrs.onRunShortcut(shortcut),
                onContextMenu: (x, y) => attrs.onShortcutContextMenu(shortcut, { x, y }),
              });
            }),
          ),
          // The pages' host: a sibling of the windows with no stacking context of its own, so a page
          // at 2i+1 and its chrome at 2i+2 interleave in the backdrop's context.
          m("div", {
            class: "live-pages pointer-events-none absolute inset-0 [&>*]:pointer-events-auto",
            oncreate: (created: m.VnodeDOM) => attrs.onPagesHostCreated(created.dom as HTMLElement),
            onbeforeupdate: () => false,
          }),
          m(
            "div",
            // Inert down to the parts that take a press (title bar, resize edges, shield, placeholders): each
            // window's chrome sits over its own page in the stacking order, and the page must get the rest.
            { class: "windows absolute inset-0 pointer-events-none" },
            // A keyed list tolerates no holes: a minimized or unknown window contributes nothing.
            placements.flatMap((placement, index) => {
              const window = windowsById.get(placement.window_id);
              if (placement.is_minimized || window === undefined) return [];
              const app = appByName(state, window.app);
              const gestureRect = store.gestureRectFor(window.id);
              return [
                m(Window, {
                  key: window.id,
                  window,
                  app,
                  title: effectiveWindowTitle(state, window, app),
                  rect: gestureRect ?? store.renderedRect(placement),
                  state: renderedState(placement, state.modes),
                  stackIndex: index,
                  isFocused: window.id === focusedWindowId,
                  isCompact: state.modes.isCompact,
                  isTouch: state.modes.isTouch,
                  isMenuOpen: attrs.openMenuWindowId === window.id,
                  isShielded: window.id !== focusedWindowId || attrs.isOverlayOpen,
                  isPlacedHere: store.isPlacedHere(window.id),
                  onStartApp:
                    app !== undefined && !app.is_running && store.canStopApp(app)
                      ? () => void store.setAppLifecycle(app.name, "start")
                      : null,
                  onRaise: () => store.raiseWindow(window.id),
                  onControl: (control, event) => attrs.onWindowControl(window.id, control, event),
                  onToggleMaximize: () => store.toggleMaximized(window.id),
                }),
              ];
            }),
          ),
          m(FloatingEntries, {
            entries: attrs.floatingEntries,
            avatar: state.avatar,
            rectOf: (entry) => store.floatingEntryRect(entry.window.app, entry.look?.position ?? null),
            openMenuWindowId: attrs.openEntryMenuWindowId,
            onClick: attrs.onEntryClick,
            onContextMenu: attrs.onEntryContextMenu,
          }),
          snapRect === null ? null : m(SnapPreview, { rect: snapRect }),
          gesture?.kind === "shortcut"
            ? shortcutGhost(gesture.iconPosition, cellRect(gesture.targetCell, metrics), appByName(state, gesture.app))
            : null,
        ],
      );
    },
  };
}

/** The lifted shortcut's icon under the pointer, and the outline of the cell it would drop into. */
function shortcutGhost(position: PixelPoint, target: PixelRect, app: AppRecord | undefined): m.Children {
  return [
    m("div", {
      "data-drop-cell": "",
      class: "pointer-events-none absolute z-(--z-sticky) rounded-lg border-2 border-dashed border-accent",
      style: { left: `${target.x}px`, top: `${target.y}px`, width: `${target.width}px`, height: `${target.height}px` },
    }),
    m(
      "div",
      {
        "data-shortcut-ghost": "",
        class:
          "pointer-events-none absolute z-(--z-sticky) flex h-(--desk-icon-size) w-(--desk-icon-size) items-center " +
          "justify-center rounded-xl bg-surface opacity-80 shadow-overlay [&>svg]:size-full",
        style: { left: `${position.x}px`, top: `${position.y}px` },
      },
      m.trust(appGlyph(app, ICON_MARKUP_SIZE)),
    ),
  ];
}
