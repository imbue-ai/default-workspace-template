/**
 * The desktop's root: the backdrop with its windows and live pages, the taskbar, the launcher
 * overlay, the floating menus and the settings dialog, wired to one ``DesktopStore``. The App
 * owns the transient interface state no record holds (which menu is open, the launcher's query,
 * the selected shortcut), measures the backdrop for the store, binds the gesture source to the
 * document, and hosts the live-page layer, reconciling it after every redraw.
 */

import m from "mithril";
import { OPEN_SHARE_SETTINGS, sendToEmbedder } from "@imbue/workspace-ui/src/embed";
import { fetchWallpapers } from "../model/api";
import { launchPathOf } from "../model/launch";
import type { AppRecord, Desktop, DesktopShortcut, LaunchPath, WallpaperListing } from "../model/records";
import type { PixelPoint } from "../geometry/frames";
import { mostRecentlyFocusedWindowOfApp, placementOf } from "../geometry/stack";
import {
  activeDesktop,
  activeFocusedWindowId,
  activePlacements,
  appByName,
  desktopById,
  openableApps,
  taskbarEntries,
  windowTitle,
} from "../reducers/desktopState";
import { nextDesktopName, nextGlyphIndex } from "../reducers/shortcuts";
import { ensureTemplateCatalogRequested, getTemplateCatalogState } from "../model/TemplateCatalog";
import type { GestureBinding, GestureListener, GestureSource } from "../gestures/pointerGestures";
import { LivePagesLayer } from "../pages/livePages";
import type { DesktopStore } from "../store/DesktopStore";
import { Backdrop } from "./Backdrop";
import { DesktopSettingsDialog, isSameWallpaper } from "./DesktopSettingsDialog";
import { LauncherOverlay, windowRowsOf } from "./LauncherOverlay";
import type { LauncherWindowRow } from "./LauncherOverlay";
import { FloatingCard, Menu, anchorForEvent, anchorForPoint } from "./Menu";
import type { MenuAnchor, MenuEntry } from "./Menu";
import { RunningAppPopover } from "./RunningAppsWidget";
import { Taskbar } from "./Taskbar";
import type { WindowControl } from "./TitleBar";
import { UpdateStalenessBanner } from "./UpdateStalenessBanner";
import { taskbarEntryMenuEntries, windowMenuEntries } from "./WindowMenu";
import { SQUIGGLE_GLYPHS } from "./squiggles";
import { shortcutLabel } from "./ShortcutIcon";

type OpenMenu =
  | { readonly kind: "window"; readonly windowId: string; readonly anchor: MenuAnchor }
  | { readonly kind: "entry"; readonly windowId: string; readonly anchor: MenuAnchor }
  | { readonly kind: "shortcut"; readonly shortcut: DesktopShortcut; readonly anchor: MenuAnchor }
  | { readonly kind: "desktops"; readonly anchor: MenuAnchor }
  | { readonly kind: "desktop"; readonly desktopId: string; readonly anchor: MenuAnchor }
  | { readonly kind: "running-app"; readonly appName: string; readonly anchor: MenuAnchor };

interface SettingsDialogState {
  readonly desktopId: string;
  readonly isDeleting: boolean;
}

export interface AppAttrs {
  readonly store: DesktopStore;
  readonly gestures: GestureSource;
  /** The shell document's host and protocol, which every page URL derives from. */
  readonly host: string;
  readonly protocol: string;
}

export function App(): m.Component<AppAttrs> {
  let openMenu: OpenMenu | null = null;
  let settingsDialog: SettingsDialogState | null = null;
  let wallpapers: WallpaperListing[] | null = null;
  let launcherQuery = "";
  let selectedShortcutKey: string | null = null;
  let pages: LivePagesLayer | null = null;
  let backdropArea: HTMLElement | null = null;
  let resizeObserver: ResizeObserver | null = null;
  let detachGestures: (() => void) | null = null;
  let store: DesktopStore | null = null;

  function closeMenu(): void {
    openMenu = null;
  }

  function closeLauncher(): void {
    store?.closeLauncher();
  }

  const onDocumentKeyDown = (event: KeyboardEvent): void => {
    if (event.key !== "Escape") return;
    if (store?.isLauncherOpen() === true) {
      closeLauncher();
      m.redraw();
    } else if (selectedShortcutKey !== null) {
      selectedShortcutKey = null;
      m.redraw();
    }
  };

  const onDocumentPointerDown = (event: Event): void => {
    if (store?.isLauncherOpen() !== true) return;
    const target = event.target;
    if (
      target instanceof Element &&
      target.closest("[data-launcher-overlay], [data-launcher-field], .modal-overlay") !== null
    )
      return;
    closeLauncher();
    m.redraw();
  };

  /** The gesture source measures points against ``root`` (the whole layout, so the taskbar's long presses
   *  count too); the store wants the backdrop's pixels, which differ by whatever sits above the backdrop. */
  function gestureListener(current: DesktopStore, root: HTMLElement): GestureListener {
    const backdropOrigin = (): DOMRect => (backdropArea ?? root).getBoundingClientRect();
    const toBackdrop = (point: PixelPoint): PixelPoint => {
      const rootBox = root.getBoundingClientRect();
      const origin = backdropOrigin();
      return { x: point.x - (origin.left - rootBox.left), y: point.y - (origin.top - rootBox.top) };
    };
    const shortcutGrabOffset = (
      binding: Extract<GestureBinding, { kind: "shortcut" }>,
      press: PixelPoint,
    ): PixelPoint => {
      const iconRect = binding.element.getBoundingClientRect();
      const origin = backdropOrigin();
      return { x: press.x - (iconRect.left - origin.left), y: press.y - (iconRect.top - origin.top) };
    };
    return {
      thresholdPx: () => current.getMetrics().dragThreshold,
      isDraggable: (binding) => {
        if (binding.kind === "shortcut") return true;
        if (binding.kind === "taskbar-entry") return false;
        return !current.getState().modes.isCompact;
      },
      onBegin: (binding, rootPoint, rootPress) => {
        pages?.setGestureActive(true);
        const point = toBackdrop(rootPoint);
        switch (binding.kind) {
          case "window-move":
            current.beginWindowMove(binding.windowId, point);
            return;
          case "window-resize":
            current.beginWindowResize(binding.windowId, binding.edge);
            return;
          case "shortcut":
            current.beginShortcutDrag(
              binding.app,
              binding.launch,
              point,
              shortcutGrabOffset(binding, toBackdrop(rootPress)),
            );
            return;
          case "taskbar-entry":
            return;
        }
      },
      onMove: (binding, rootPoint, delta) => {
        const point = toBackdrop(rootPoint);
        switch (binding.kind) {
          case "window-move":
            current.updateWindowMove(point);
            return;
          case "window-resize":
            current.updateWindowResize(delta);
            return;
          case "shortcut":
            current.updateShortcutDrag(point);
            return;
          case "taskbar-entry":
            return;
        }
      },
      onEnd: (binding, rootPoint, delta) => {
        const point = toBackdrop(rootPoint);
        switch (binding.kind) {
          case "window-move":
            current.endWindowMove(point);
            break;
          case "window-resize":
            current.endWindowResize(delta);
            break;
          case "shortcut":
            current.endShortcutDrag(point);
            break;
          case "taskbar-entry":
            break;
        }
        pages?.setGestureActive(false);
      },
      onCancel: () => {
        current.cancelGesture();
        pages?.setGestureActive(false);
      },
      onLongPress: (binding, client) => {
        const anchor = anchorForPoint(client.x, client.y);
        switch (binding.kind) {
          case "window-move":
          case "window-resize":
            openMenu = { kind: "window", windowId: binding.windowId, anchor };
            break;
          case "shortcut": {
            const shortcut = activeDesktop(current.getState())?.shortcuts.find(
              (candidate) => candidate.target.app === binding.app && candidate.target.launch === binding.launch,
            );
            if (shortcut !== undefined) openMenu = { kind: "shortcut", shortcut, anchor };
            break;
          }
          case "taskbar-entry":
            openMenu = { kind: "entry", windowId: binding.windowId, anchor };
            break;
        }
        m.redraw();
      },
    };
  }

  function windowMenu(current: DesktopStore, windowId: string, anchor: MenuAnchor): m.Children {
    const state = current.getState();
    const window = activeDesktop(state)?.windows.find((candidate) => candidate.id === windowId);
    if (window === undefined) return null;
    const app = appByName(state, window.app);
    const entries = windowMenuEntries(app, {
      refresh: () => current.refreshWindow(windowId),
      share:
        app === undefined || app.critical
          ? null
          : () => sendToEmbedder(OPEN_SHARE_SETTINGS, { serviceName: app.name }),
      setAppLifecycle:
        app !== undefined && current.canStopApp(app)
          ? (action) => void current.setAppLifecycle(app.name, action)
          : null,
      close: () => void current.closeWindow(windowId),
    });
    return m(Menu, {
      anchor,
      placement: "below",
      marker: "window-menu",
      entries,
      onClose: closeMenu,
      isInsideTrigger: isWindowMenuButton,
    });
  }

  /** The kebab that opened the window menu: its press must not close the card before its click toggles it. */
  function isWindowMenuButton(target: Node): boolean {
    return target instanceof Element && target.closest('[data-window-control="menu"]') !== null;
  }

  function entryMenu(current: DesktopStore, windowId: string, anchor: MenuAnchor): m.Children {
    const state = current.getState();
    if (!activeDesktop(state)?.windows.some((candidate) => candidate.id === windowId)) return null;
    const placement = placementOf(state.layout, windowId);
    const entries = taskbarEntryMenuEntries(
      {
        isMinimized: placement.is_minimized,
        isMaximized: placement.state === "MAXIMIZED",
        restore: () => current.restoreWindow(windowId),
        minimize: () => current.minimizeWindow(windowId),
        maximize: () => current.setWindowState(windowId, "MAXIMIZED"),
        unmaximize: () => current.toggleMaximized(windowId),
        close: () => void current.closeWindow(windowId),
      },
      state.modes.isCompact,
    );
    return m(Menu, { anchor, placement: "below", marker: "entry-menu", entries, onClose: closeMenu });
  }

  function shortcutMenu(current: DesktopStore, opened: DesktopShortcut, anchor: MenuAnchor): m.Children {
    const state = current.getState();
    const desktop = activeDesktop(state);
    // The record as it is now (its mode may have flipped elsewhere), and nothing once it is removed.
    const shortcut = desktop?.shortcuts.find(
      (candidate) => candidate.target.app === opened.target.app && candidate.target.launch === opened.target.launch,
    );
    if (desktop === null || shortcut === undefined) return null;
    const app = appByName(state, shortcut.target.app);
    const launchPath = app === undefined ? null : launchPathOf(app, shortcut.target.launch);
    const recent = app === undefined ? null : mostRecentlyFocusedWindowOfApp(state.layout, desktop, app.name);
    const otherMode = shortcut.mode === "focus" ? "new" : "focus";
    const entries: MenuEntry[] = [{ key: "open", label: "Open", run: () => void current.runShortcut(shortcut) }];
    if (shortcut.mode === "focus" && launchPath !== null) {
      entries.push({
        key: "open-new",
        label: launchPath.label,
        run: () => void current.runLaunch(shortcut.target.app, shortcut.target.launch, "new"),
      });
    } else if (shortcut.mode === "new" && app !== undefined) {
      entries.push({
        key: "focus-last",
        label: `Focus last ${app.display_name}`,
        isDisabled: recent === null,
        run: () => {
          if (recent !== null) current.raiseWindow(recent.id);
        },
      });
    }
    entries.push(
      {
        key: "change-mode",
        label: `Change shortcut to "${shortcutLabel({ ...shortcut, mode: otherMode }, app)}"`,
        run: () => void current.setShortcut(desktop.id, { ...shortcut, mode: otherMode }),
      },
      "divider",
      {
        key: "remove",
        label: "Remove",
        iconName: "trash",
        isDestructive: true,
        run: () => void current.removeShortcut(shortcut.target.app, shortcut.target.launch),
      },
    );
    return m(Menu, { anchor, placement: "below", marker: "shortcut-menu", entries, onClose: closeMenu });
  }

  function desktopsMenu(current: DesktopStore, anchor: MenuAnchor): m.Children {
    const state = current.getState();
    const active = state.activeDesktopId;
    const entries: MenuEntry[] = [
      {
        key: "new-desktop",
        label: "New desktop",
        run: () => {
          const glyphIndex = nextGlyphIndex(
            state.desktops.map((desktop) => desktop.glyph),
            SQUIGGLE_GLYPHS.length,
          );
          void current.createDesktop(nextDesktopName(state.desktops), SQUIGGLE_GLYPHS[glyphIndex].color, glyphIndex);
        },
      },
      "divider",
      {
        key: "settings",
        label: "Desktop settings...",
        isDisabled: active === null,
        run: () => openSettings(active, false),
      },
      {
        key: "delete",
        label: "Delete desktop...",
        isDestructive: true,
        isDisabled: active === null,
        run: () => openSettings(active, true),
      },
    ];
    return m(Menu, {
      anchor,
      placement: "below",
      marker: "desktops-menu",
      entries,
      onClose: closeMenu,
      isInsideTrigger: isDesktopsMenuButton,
    });
  }

  function desktopMenu(current: DesktopStore, desktopId: string, anchor: MenuAnchor): m.Children {
    const entries: MenuEntry[] = [
      { key: "switch", label: "Switch to this desktop", run: () => void current.switchDesktop(desktopId) },
      { key: "settings", label: "Settings...", run: () => openSettings(desktopId, false) },
      "divider",
      { key: "delete", label: "Delete...", isDestructive: true, run: () => openSettings(desktopId, true) },
    ];
    return m(Menu, { anchor, placement: "below", marker: "desktop-menu", entries, onClose: closeMenu });
  }

  function isDesktopsMenuButton(target: Node): boolean {
    return target instanceof Element && target.closest("[data-desktops-menu]") !== null;
  }

  function isRunningAppButton(target: Node): boolean {
    return target instanceof Element && target.closest("[data-running-app]") !== null;
  }

  function runningAppPopover(current: DesktopStore, appName: string, anchor: MenuAnchor): m.Children {
    const state = current.getState();
    const app = appByName(state, appName);
    const desktop = activeDesktop(state);
    if (app === undefined || desktop === null) return null;
    const placements = activePlacements(state);
    const windows = desktop.windows
      .filter((window) => window.app === app.name)
      .map((window) => ({
        window,
        title: windowTitle(window, app),
        isMinimized: placements.find((placement) => placement.window_id === window.id)?.is_minimized ?? true,
      }));
    return m(
      FloatingCard,
      {
        anchor,
        placement: "below",
        role: "dialog",
        marker: "running-app",
        onClose: closeMenu,
        isInsideTrigger: isRunningAppButton,
      },
      m(RunningAppPopover, {
        app,
        windows,
        onPickWindow: (windowId) => {
          closeMenu();
          current.restoreWindow(windowId);
        },
        onRunLaunch: (launchPath: LaunchPath) => {
          closeMenu();
          void current.openLaunchPath(app.name, launchPath.id, {});
        },
        onAddShortcut: (launchPath: LaunchPath) => {
          void current.addShortcut(app.name, launchPath.id, "focus");
        },
      }),
    );
  }

  function openSettings(desktopId: string | null, isDeleting: boolean): void {
    if (desktopId === null) return;
    settingsDialog = { desktopId, isDeleting };
    if (wallpapers === null) {
      void fetchWallpapers()
        .then((listed) => {
          wallpapers = listed;
        })
        .catch((error: unknown) => {
          console.warn("[si] could not list the wallpapers", error);
          wallpapers = [];
        })
        .finally(() => m.redraw());
    }
  }

  function settingsDialogView(current: DesktopStore, dialog: SettingsDialogState): m.Children {
    const desktop = desktopById(current.getState(), dialog.desktopId);
    if (desktop === null) return null;
    return m(DesktopSettingsDialog, {
      desktop,
      wallpapers,
      isDeleting: dialog.isDeleting,
      onSave: async (name, color, glyph, sharing, wallpaper) => {
        await current.updateDesktopSettings(desktop.id, name, color, glyph, sharing);
        if (!isSameWallpaper(wallpaper, desktop.wallpaper)) await current.setDesktopWallpaper(desktop.id, wallpaper);
        settingsDialog = null;
      },
      onDelete: async () => {
        await current.deleteDesktop(desktop.id);
        settingsDialog = null;
      },
      onCancel: () => {
        settingsDialog = null;
      },
    });
  }

  function launcherRows(current: DesktopStore): LauncherWindowRow[] {
    const state = current.getState();
    const placements = activePlacements(state);
    return windowRowsOf(
      state.desktops,
      state.activeDesktopId,
      (name) => appByName(state, name),
      windowTitle,
      (desktopId, windowId) =>
        desktopId === state.activeDesktopId
          ? (placements.find((placement) => placement.window_id === windowId)?.is_minimized ?? true)
          : false,
    );
  }

  function runLaunchFromLauncher(
    current: DesktopStore,
    app: AppRecord,
    launchPath: LaunchPath,
    params: Readonly<Record<string, string>>,
  ): void {
    closeLauncher();
    launcherQuery = "";
    void current.openLaunchPath(app.name, launchPath.id, params);
  }

  function pickWindowFromLauncher(current: DesktopStore, row: LauncherWindowRow): void {
    closeLauncher();
    launcherQuery = "";
    if (row.desktopId !== current.getState().activeDesktopId) {
      void current.switchDesktop(row.desktopId).then(() => current.restoreWindow(row.window.id));
    } else {
      current.restoreWindow(row.window.id);
    }
  }

  function onWindowControl(current: DesktopStore, windowId: string, control: WindowControl, event: MouseEvent): void {
    switch (control) {
      case "minimize":
        current.minimizeWindow(windowId);
        return;
      case "maximize":
        current.setWindowState(windowId, "MAXIMIZED");
        return;
      case "restore":
        current.toggleMaximized(windowId);
        return;
      case "close":
        void current.closeWindow(windowId);
        return;
      case "menu":
        openMenu =
          openMenu?.kind === "window" && openMenu.windowId === windowId
            ? null
            : { kind: "window", windowId, anchor: anchorForEvent(event) };
        return;
    }
  }

  return {
    oncreate(vnode) {
      store = vnode.attrs.store;
      document.addEventListener("keydown", onDocumentKeyDown);
      document.addEventListener("pointerdown", onDocumentPointerDown, true);
      const root = vnode.dom as HTMLElement;
      detachGestures = vnode.attrs.gestures.attach(root, gestureListener(vnode.attrs.store, root));
      ensureTemplateCatalogRequested();
    },
    onupdate() {
      pages?.reconcile();
    },
    onremove() {
      document.removeEventListener("keydown", onDocumentKeyDown);
      document.removeEventListener("pointerdown", onDocumentPointerDown, true);
      detachGestures?.();
      resizeObserver?.disconnect();
    },
    view(vnode) {
      const current = vnode.attrs.store;
      store = current;
      const state = current.getState();
      const desktop: Desktop | null = activeDesktop(state);
      const placements = activePlacements(state);
      const focused = activeFocusedWindowId(state);
      const isLauncherOpen = current.isLauncherOpen();
      return m("div", { class: "app-layout flex h-screen flex-col bg-page" }, [
        m(UpdateStalenessBanner),
        m(
          "div",
          {
            "data-backdrop-area": "",
            class: "backdrop-area relative min-h-0 flex-1 overflow-hidden",
            oncreate: (created: m.VnodeDOM) => {
              backdropArea = created.dom as HTMLElement;
              const measure = (): void => {
                const box = backdropArea?.getBoundingClientRect();
                if (box !== undefined) current.setBackdropSize({ width: box.width, height: box.height });
              };
              resizeObserver = new ResizeObserver(measure);
              resizeObserver.observe(backdropArea);
              measure();
            },
          },
          [
            desktop === null
              ? m(
                  "div",
                  { class: "flex h-full items-center justify-center text-(length:--font-size-row) text-faint" },
                  state.isDesktopsLoaded ? "No desktop yet." : "Loading…",
                )
              : m(Backdrop, {
                  store: current,
                  desktop,
                  placements,
                  focusedWindowId: focused,
                  selectedShortcutKey,
                  openMenuWindowId: openMenu?.kind === "window" ? openMenu.windowId : null,
                  onSelectShortcut: (key) => {
                    selectedShortcutKey = key;
                  },
                  onRunShortcut: (shortcut) => void current.runShortcut(shortcut),
                  onShortcutContextMenu: (shortcut, point) => {
                    openMenu = { kind: "shortcut", shortcut, anchor: anchorForPoint(point.x, point.y) };
                  },
                  onWindowControl: (windowId, control, event) => onWindowControl(current, windowId, control, event),
                  onPagesHostCreated: (host) => {
                    pages = new LivePagesLayer(host, current, {
                      host: vnode.attrs.host,
                      protocol: vnode.attrs.protocol,
                    });
                    pages.start();
                  },
                }),
            isLauncherOpen
              ? m(LauncherOverlay, {
                  query: launcherQuery,
                  apps: openableApps(state),
                  windows: launcherRows(current),
                  activeDesktopId: state.activeDesktopId,
                  catalog: getTemplateCatalogState(),
                  isCompact: state.modes.isCompact,
                  onRunLaunch: (app, launchPath, params) => runLaunchFromLauncher(current, app, launchPath, params),
                  onPickWindow: (row) => pickWindowFromLauncher(current, row),
                })
              : null,
          ],
        ),
        m(Taskbar, {
          entries: taskbarEntries(state),
          isCompact: state.modes.isCompact,
          openEntryMenuWindowId: openMenu?.kind === "entry" ? openMenu.windowId : null,
          launcher: {
            query: launcherQuery,
            isOpen: isLauncherOpen,
            isCompact: state.modes.isCompact,
            onOpen: () => current.openLauncher(),
            onClose: closeLauncher,
            onQuery: (query) => {
              launcherQuery = query;
            },
          },
          tray: {
            desktops: state.desktops,
            activeDesktopId: state.activeDesktopId,
            apps: state.apps,
            isDesktopsMenuOpen: openMenu?.kind === "desktops",
            openRunningAppName: openMenu?.kind === "running-app" ? openMenu.appName : null,
            onSwitchDesktop: (desktopId) => void current.switchDesktop(desktopId),
            onOpenDesktopsMenu: (event) => {
              openMenu = openMenu?.kind === "desktops" ? null : { kind: "desktops", anchor: anchorForEvent(event) };
            },
            onDesktopContextMenu: (desktopId, x, y) => {
              openMenu = { kind: "desktop", desktopId, anchor: anchorForPoint(x, y) };
            },
            onOpenRunningApp: (app, event) => {
              openMenu =
                openMenu?.kind === "running-app" && openMenu.appName === app.name
                  ? null
                  : { kind: "running-app", appName: app.name, anchor: anchorForEvent(event) };
            },
          },
          onEntryClick: (windowId) => current.toggleTaskbarEntry(windowId),
          onEntryContextMenu: (windowId, x, y) => {
            openMenu = { kind: "entry", windowId, anchor: anchorForPoint(x, y) };
          },
        }),
        openMenu?.kind === "window" ? windowMenu(current, openMenu.windowId, openMenu.anchor) : null,
        openMenu?.kind === "entry" ? entryMenu(current, openMenu.windowId, openMenu.anchor) : null,
        openMenu?.kind === "shortcut" ? shortcutMenu(current, openMenu.shortcut, openMenu.anchor) : null,
        openMenu?.kind === "desktops" ? desktopsMenu(current, openMenu.anchor) : null,
        openMenu?.kind === "desktop" ? desktopMenu(current, openMenu.desktopId, openMenu.anchor) : null,
        openMenu?.kind === "running-app" ? runningAppPopover(current, openMenu.appName, openMenu.anchor) : null,
        settingsDialog === null ? null : settingsDialogView(current, settingsDialog),
      ]);
    },
  };
}
