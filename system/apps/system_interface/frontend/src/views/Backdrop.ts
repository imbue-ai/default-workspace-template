/**
 * The backdrop (concepts.md section 2.4): over the active desktop's wallpaper, its shortcut grid
 * fitted to the current backdrop at render time, its windows in stacking order (each over the
 * live page the pages layer positions for it), the floating pinned entries above them, and the snap
 * preview. A shortcut being dragged needs no layer of its own: the icon carries its own lift, and the
 * store's placement carries the room made for it. Every pixel comes from the store's geometry; nothing
 * here measures the DOM or listens for gestures.
 */

import m from "mithril";
import { cellRect } from "../geometry/grid";
import type { PixelPoint } from "../geometry/frames";
import type { Desktop, DesktopShortcut, Placement } from "../model/records";
import { shortcutKey } from "../model/records";
import { appByName, effectiveWindowTitle, renderedState } from "../reducers/desktopState";
import type { TaskbarEntry } from "../reducers/desktopState";
import type { DesktopStore } from "../store/DesktopStore";
import { FloatingEntries } from "./FloatingEntries";
import { ShortcutIcon } from "./ShortcutIcon";
import { SnapPreview } from "./SnapPreview";
import { Window } from "./Window";
import type { WindowControl } from "./TitleBar";

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
  /** Spread onto a window's maximize control: resting on it opens that window's size menu. */
  readonly sizeMenuTrigger: (windowId: string) => m.Attributes;
  readonly onEntryClick: (windowId: string) => void;
  readonly onEntryContextMenu: (windowId: string, x: number, y: number, target: Element) => void;
  /** Whether a menu or the launcher is open: every window is shielded, so the press that closes it reaches the shell. */
  readonly isOverlayOpen: boolean;
  readonly onSelectShortcut: (key: string | null) => void;
  readonly onRunShortcut: (shortcut: DesktopShortcut) => void;
  readonly onShortcutContextMenu: (shortcut: DesktopShortcut, point: PixelPoint, target: Element) => void;
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
      // The placement already carries the drag: the held shortcut sits in the cell under the pointer (its
      // lift translates it there from the cell it came from) and the shortcut that was there has stepped aside.
      const placed = store.placedShortcuts();
      const held = gesture !== null && gesture.kind === "shortcut" ? gesture : null;
      const heldKey = held === null ? null : shortcutKey(held.app, held.launch);
      const windowsById = new Map(desktop.windows.map((window) => [window.id, window]));
      const snapRect = store.snapPreviewRect();

      return m(
        "div",
        {
          "data-desktop-id": desktop.id,
          // The wallpaper is the app layout's, painted across the whole viewport so the taskbar has
          // something to blur; this layer stays clear of it.
          class: "backdrop relative isolate h-full w-full overflow-hidden select-none",
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
              // The one in the hand is drawn in the cell it came from and translated out of it, so the icon
              // stays exactly where it was grabbed whatever cell the room being made has put it in.
              const lifted = heldKey === key ? held : null;
              return m(ShortcutIcon, {
                key,
                shortcut,
                cell,
                rect: cellRect(lifted === null ? cell : lifted.originCell, metrics),
                app: appByName(state, shortcut.target.app),
                isAppsLoaded: state.isAppsLoaded,
                isSelected: attrs.selectedShortcutKey === key,
                lift: lifted === null ? null : lifted.lift,
                isSliding: held !== null,
                isRunOnClick: state.modes.isTouch,
                onSelect: () => attrs.onSelectShortcut(key),
                onRun: () => attrs.onRunShortcut(shortcut),
                onContextMenu: (x, y, target) => attrs.onShortcutContextMenu(shortcut, { x, y }, target),
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
              return [
                m(Window, {
                  key: window.id,
                  window,
                  app,
                  title: effectiveWindowTitle(state, window, app),
                  rect: store.windowRect(window.id),
                  state: renderedState(placement, state.modes),
                  stackIndex: index,
                  isFocused: window.id === focusedWindowId,
                  isCompact: state.modes.isCompact,
                  isTouch: state.modes.isTouch,
                  isMenuOpen: attrs.openMenuWindowId === window.id,
                  sizeMenuTrigger: attrs.sizeMenuTrigger(window.id),
                  isShielded: window.id !== focusedWindowId || attrs.isOverlayOpen,
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
            rectOf: (entry) => store.renderedFloatingEntryRect(entry.window.app, entry.look?.position ?? null),
            openMenuWindowId: attrs.openEntryMenuWindowId,
            onClick: attrs.onEntryClick,
            onContextMenu: attrs.onEntryContextMenu,
          }),
          m(SnapPreview, { rect: snapRect }),
        ],
      );
    },
  };
}
