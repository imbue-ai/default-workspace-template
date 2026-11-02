/**
 * The backdrop (concepts.md section 2.4): over the active desktop's wallpaper, its shortcut grid
 * fitted to the current backdrop at render time, its windows in stacking order (each over the
 * live page the pages layer positions for it), the floating pinned entries above them, and the snap
 * preview. A shortcut being dragged needs no layer of its own: the icon carries its own lift, and the
 * store's placement carries the room made for it. Every pixel comes from the store's geometry; nothing
 * here measures the DOM or listens for gestures. A right-click on the bare backdrop asks for the desktop's
 * menu (the multiplayer spec, section 8.2). While the overview is up it stands in for the windows layer, so
 * the live-page layer lays the pages over its tiles.
 *
 * The grid, the pages' host, and the windows sit on one stage, which is inert while a desktop that is not the
 * viewer's own is viewed (section 8.3): nothing on it takes a press, a key, or focus. Viewing a desktop whose
 * owner's backdrop size is known draws the stage at that size, scaled down to fit and centred, with the desktop's
 * wallpaper on it and a band in the owner's colour around it; the floating entries stay the viewer's own, outside it.
 */

import m from "mithril";
import { targetElementOf } from "@imbue/workspace-ui/src/context_menu_rows";
import { cellRect } from "../geometry/grid";
import type { PixelPoint } from "../geometry/frames";
import type { Letterbox } from "../geometry/letterbox";
import { wallpaperBackgroundImage } from "../model/api";
import type { Desktop, DesktopShortcut, Placement } from "../model/records";
import { appChatLaunchOf } from "../model/launch";
import { shortcutKey } from "../model/records";
import { appByName, effectiveWindowTitle, isAppShownStopped } from "../reducers/desktopState";
import type { TaskbarEntry } from "../reducers/desktopState";
import type { DesktopStore } from "../store/DesktopStore";
import { DetachedWindowGhost } from "./DetachedWindowGhost";
import { FloatingEntries } from "./FloatingEntries";
import { DESKTOP_ROOT_ATTRIBUTE } from "../pages/livePages";
import { Overview } from "./Overview";
import type { OverviewAttrs } from "./Overview";
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
  /** A right-click on the bare backdrop: the desktop's menu. */
  readonly onBackdropContextMenu: (point: PixelPoint, target: Element) => void;
  /** The overview over the backdrop, or null while it is closed. */
  readonly overview: OverviewAttrs | null;
  /** The letterbox a viewed desktop is drawn in, and the colour of the band around it; null draws the desktop
   *  edge to edge. */
  readonly letterbox: BackdropLetterbox | null;
  readonly onWindowControl: (windowId: string, control: WindowControl, event: MouseEvent) => void;
  /** A pulled-out window's ghost was asked to show its own desktop window, to hide itself, or to bring the
   *  window back. */
  readonly onShowDetachedWindow: (windowId: string) => void;
  readonly onHideWindowGhost: (windowId: string) => void;
  readonly onBringBackWindow: (windowId: string) => void;
  /** The element the live pages are appended to, created once and never re-rendered. */
  readonly onPagesHostCreated: (host: HTMLElement) => void;
}

/** How a viewed desktop is drawn (the multiplayer spec, section 8.3): where its owner's backdrop lands in this
 *  one, and the owner's colour for the band around it. */
export interface BackdropLetterbox {
  readonly box: Letterbox;
  readonly color: string;
}

/** How much of the owner's colour the band around a letterboxed desktop wears over the page surface. */
const LETTERBOX_BAND_TINT = "12%";

/** The stage's placement: the letterbox's one transform from the top-left corner, else the whole backdrop. */
function stageStyle(desktop: Desktop, letterbox: BackdropLetterbox | null): Record<string, string> {
  if (letterbox === null) return { width: "100%", height: "100%" };
  const { size, scale, offset } = letterbox.box;
  const wallpaper: Record<string, string> =
    desktop.wallpaper == null ? {} : { backgroundImage: wallpaperBackgroundImage(desktop.wallpaper) };
  return {
    width: `${size.width}px`,
    height: `${size.height}px`,
    transformOrigin: "top left",
    transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
    ...wallpaper,
  };
}

export function Backdrop(): m.Component<BackdropAttrs> {
  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const { store, desktop, placements, focusedWindowId } = attrs;
      const state = store.getState();
      const isAppChatOffered = appChatLaunchOf(state.apps) !== null;
      const metrics = store.getMetrics();
      const gesture = store.getGesture();
      // The placement already carries the drag: the held shortcut sits in the cell under the pointer (its
      // lift translates it there from the cell it came from) and the shortcut that was there has stepped aside.
      const placed = store.placedShortcuts();
      const held = gesture !== null && gesture.kind === "shortcut" ? gesture : null;
      const heldKey = held === null ? null : shortcutKey(held.app, held.launch);
      const windowsById = new Map(desktop.windows.map((window) => [window.id, window]));
      const snapRect = store.snapPreviewRect();
      const letterbox = attrs.letterbox;
      const isViewing = store.viewedDesktop() !== null;
      const immersive = store.getImmersive();

      return m(
        "div",
        {
          [DESKTOP_ROOT_ATTRIBUTE]: desktop.id,
          // The wallpaper is the app layout's, painted across the whole viewport so the taskbar has
          // something to blur; this layer stays clear of it, except for the band around a letterboxed desktop,
          // whose wallpaper is the stage's.
          class: "backdrop relative isolate h-full w-full overflow-hidden select-none",
          style:
            letterbox === null
              ? {}
              : { backgroundColor: `color-mix(in srgb, ${letterbox.color} ${LETTERBOX_BAND_TINT}, var(--c-bg))` },
        },
        [
          m(
            "div",
            {
              class:
                "desktop-stage absolute top-0 left-0" +
                (letterbox === null
                  ? ""
                  : " bg-(--desk-backdrop) bg-cover bg-center bg-(image:--desk-default-wallpaper)"),
              style: stageStyle(desktop, letterbox),
              "data-viewing-letterbox": letterbox === null ? undefined : String(letterbox.box.scale),
              // A viewed desktop is looked at, never arranged (the multiplayer spec, section 8.3): the stage holds
              // the grid, the pages, the windows, and the ghosts, and inert takes the pointer, the keyboard, and
              // focus from all of them at once; the band, the chip, the tray, and the taskbar lie outside it, so
              // "Stop viewing" stays reachable. A boolean, so mithril sets the property where the browser has one
              // and the attribute where it does not.
              inert: isViewing,
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
                  oncontextmenu: (event: MouseEvent) => {
                    if (event.target !== event.currentTarget) return;
                    event.preventDefault();
                    attrs.onBackdropContextMenu({ x: event.clientX, y: event.clientY }, targetElementOf(event));
                  },
                },
                placed.map(({ shortcut, cell }) => {
                  const key = shortcutKey(shortcut.target.app, shortcut.target.launch);
                  const app = appByName(state, shortcut.target.app);
                  // The one in the hand is drawn in the cell it came from and translated out of it, so the icon
                  // stays exactly where it was grabbed whatever cell the room being made has put it in.
                  const lifted = heldKey === key ? held : null;
                  return m(ShortcutIcon, {
                    key,
                    shortcut,
                    cell,
                    rect: cellRect(lifted === null ? cell : lifted.originCell, metrics),
                    app,
                    isStopped: isAppShownStopped(state, app),
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
                class: "live-pages pointer-events-none absolute inset-0 outline-none [&>*]:pointer-events-auto",
                oncreate: (created: m.VnodeDOM) => attrs.onPagesHostCreated(created.dom as HTMLElement),
                onbeforeupdate: () => false,
              }),
              // The overview stands in for the windows: its tiles are where the pages lie while it is up.
              attrs.overview !== null
                ? null
                : m(
                    "div",
                    // Inert down to the parts that take a press (title bar, resize edges, shield, placeholders): each
                    // window's chrome sits over its own page in the stacking order, and the page must get the rest.
                    // While a desktop that is not the viewer's own is viewed, the stage is inert and the layer
                    // wears ``windows-viewing`` (style.css) for the look of it: the owner's windows are looked at,
                    // never arranged.
                    {
                      class: "windows absolute inset-0 pointer-events-none" + (isViewing ? " windows-viewing" : ""),
                    },
                    // A keyed list tolerates no holes: a minimized or unknown window contributes nothing (a pulled-out
                    // window's hidden ghost included).
                    placements.flatMap((placement, index): m.Children[] => {
                      const window = windowsById.get(placement.window_id);
                      if (placement.is_minimized || window === undefined) return [];
                      const app = appByName(state, window.app);
                      // A pulled-out window leaves its ghost at its frame, where a return lands, whatever state it was
                      // pulled out in (detach keeps the state); its page is in the chrome's own window.
                      if (placement.is_detached) {
                        return [
                          m(DetachedWindowGhost, {
                            key: window.id,
                            window,
                            app,
                            title: effectiveWindowTitle(state, window, app),
                            rect: store.renderedRect({ ...placement, state: "NORMAL" }),
                            stackIndex: index,
                            onShow: () => attrs.onShowDetachedWindow(window.id),
                            onHide: () => attrs.onHideWindowGhost(window.id),
                            onBringBack: () => attrs.onBringBackWindow(window.id),
                          }),
                        ];
                      }
                      return [
                        m(Window, {
                          key: window.id,
                          window,
                          app,
                          title: effectiveWindowTitle(state, window, app),
                          rect: store.windowRect(window.id),
                          state: placement.state,
                          stackIndex: index,
                          isFocused: window.id === focusedWindowId,
                          isTouch: state.modes.isTouch,
                          isMenuOpen: attrs.openMenuWindowId === window.id,
                          hasAppChat: isAppChatOffered && app !== undefined && !app.internal,
                          sizeMenuTrigger: attrs.sizeMenuTrigger(window.id),
                          isShielded: window.id !== focusedWindowId || attrs.isOverlayOpen,
                          isStopped: isAppShownStopped(state, app),
                          immersive: store.getCanImmerse()
                            ? {
                                isOn: immersive !== null,
                                isKeyboardLocked: immersive?.isKeyboardLocked === true,
                                onEnter: () => store.requestImmersive(window.id),
                                onLeave: () => store.leaveImmersive(window.id),
                              }
                            : null,
                          onRaise: () => store.raiseWindow(window.id),
                          onControl: (control, event) => attrs.onWindowControl(window.id, control, event),
                          onToggleMaximize: () => store.toggleMaximized(window.id),
                          chrome: store.shownChrome(),
                        }),
                      ];
                    }),
                  ),
            ],
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
          attrs.overview === null ? null : m(Overview, attrs.overview),
        ],
      );
    },
  };
}
