/**
 * The desktop's root: the backdrop with its windows and live pages, the taskbar, the launcher
 * menu, the floating menus, the settings dialog, and the avatar chooser, wired to one
 * ``DesktopStore``. The App owns the transient interface state no record holds (which menu is
 * open, the launcher's query and highlight, the selected shortcut, the chooser and the designs it lists),
 * measures the backdrop for the store, binds the gesture source to the document, and hosts the
 * live-page layer, reconciling it after every redraw.
 */

import m from "mithril";
import { createMenu } from "@imbue/workspace-ui/src/components/menu";
import type { MenuRow } from "@imbue/workspace-ui/src/components/menu";
import { anchorForEvent, anchorForPoint } from "@imbue/workspace-ui/src/menu-position";
import type { MenuAlign, MenuAnchor } from "@imbue/workspace-ui/src/menu-position";
import { OPEN_SHARE_SETTINGS, sendToEmbedder } from "@imbue/workspace-ui/src/embed";
import { fetchWallpapers, wallpaperImageUrl } from "../model/api";
import { launchPathOf } from "../model/launch";
import type { AvatarDesign, Desktop, DesktopShortcut, WallpaperListing } from "../model/records";
import { shortcutKey } from "../model/records";
import type { PixelPoint } from "../geometry/frames";
import { mostRecentlyFocusedWindowOfApp, placementOf } from "../geometry/stack";
import {
  activeDesktop,
  activeFocusedWindowId,
  activePlacements,
  appByName,
  barEntries,
  desktopById,
  draftTargetOf,
  entryLook,
  floatingEntries,
  pinnedWindowOf,
} from "../reducers/desktopState";
import {
  defaultHighlightIndex,
  isRowEnabled,
  launcherRowsOf,
  moveHighlight,
  secondaryTextRow,
} from "../reducers/launcherRows";
import type { LauncherMenuRows, LauncherRow } from "../reducers/launcherRows";
import { nextDesktopName, nextGlyphIndex } from "../reducers/shortcuts";
import { PINNED_ENTRY_ATTRIBUTE } from "../gestures/pointerGestures";
import type { GestureListener, GestureSource } from "../gestures/pointerGestures";
import { LivePagesLayer, WINDOW_ID_ATTRIBUTE } from "../pages/livePages";
import type { DesktopStore } from "../store/DesktopStore";
import { AVATAR_DESIGN_PROMPT, AvatarChooserDialog } from "./AvatarChooserDialog";
import { Backdrop } from "./Backdrop";
import { DesktopSettingsDialog, isSameWallpaper } from "./DesktopSettingsDialog";
import { LauncherMenu } from "./LauncherMenu";
import { ReplacedDesktopNotice } from "./ReplacedDesktopNotice";
import { applyRectStyle } from "./pixelStyle";
import { SNAP_PREVIEW_ATTRIBUTE, applySnapPreviewStyle } from "./SnapPreview";
import { Taskbar } from "./Taskbar";
import type { WindowControl } from "./TitleBar";
import { UpdateNoticeBanner } from "./UpdateNoticeBanner";
import { UpdateStalenessBanner } from "./UpdateStalenessBanner";
import { taskbarEntryMenuRows, windowMenuRows } from "./WindowMenu";
import { windowSizeRow } from "./WindowSizeRow";
import type { WindowSizeActions } from "./WindowSizeRow";
import { SQUIGGLE_GLYPHS } from "./squiggles";

/** Which menu is open and what it was opened for. Where it sits, and everything about taking it
 *  down, belongs to the menu component itself. */
type OpenMenu =
  | { readonly kind: "window"; readonly windowId: string }
  | { readonly kind: "size"; readonly windowId: string }
  | { readonly kind: "entry"; readonly windowId: string }
  | { readonly kind: "shortcut"; readonly shortcut: DesktopShortcut }
  | { readonly kind: "desktops" }
  | { readonly kind: "desktop"; readonly desktopId: string };

/** The width the desktop's menus never go under, so a menu of two-word verbs is still a card. */
const MENU_MIN_WIDTH = 176;

/** Set on the desktop's root while a window is being dragged by its title bar, for as long as the
 *  pointer should hold the closed hand (style.css). Written straight onto the element: the drag
 *  paints without redrawing, and mithril leaves an attribute no vnode carries alone. */
const WINDOW_DRAGGING_ATTRIBUTE = "data-window-dragging";

/** Set on the desktop's root for the length of a press, which is longer than the drag it may
 *  become, so a window the pointer carries is held by it rather than trailing it (style.css). The
 *  snap a release commits is written after the press has ended, and travels the last step. */
const WINDOW_MOTION_ATTRIBUTE = "data-window-motion";

interface SettingsDialogState {
  readonly desktopId: string;
  readonly isDeleting: boolean;
}

interface AvatarChooserState {
  /** The designs the chooser lists; null until the catalog answers. */
  readonly designs: readonly AvatarDesign[] | null;
  readonly loadError: string | null;
}

export interface AppAttrs {
  readonly store: DesktopStore;
  readonly gestures: GestureSource;
  /** The shell document's host and protocol, which every page URL derives from. */
  readonly host: string;
  readonly protocol: string;
}

/** Whether the secondary chord reads as Cmd+Enter: the browser runs on an Apple platform. */
function isApplePlatform(): boolean {
  return /Mac|iPhone|iPad|iPod/.test(navigator.platform) || /Mac OS/.test(navigator.userAgent);
}

export function App(): m.Component<AppAttrs> {
  let openMenu: OpenMenu | null = null;
  let settingsDialog: SettingsDialogState | null = null;
  let avatarChooser: AvatarChooserState | null = null;
  let wallpapers: WallpaperListing[] | null = null;
  let launcherQuery = "";
  // The row the arrows or a hover moved the highlight to; null follows the default rule for the rows shown.
  let launcherHighlight: number | null = null;
  // How far the launcher field stands above its one row, in px; the menu sits above it.
  let launcherFieldRise = 0;
  let selectedShortcutKey: string | null = null;
  let pages: LivePagesLayer | null = null;
  let backdropArea: HTMLElement | null = null;
  let resizeObserver: ResizeObserver | null = null;
  /** How many of a travelling window's properties are still in transition, by window id. A count
   *  rather than a flag: a move runs one transition per property it changes, each ending on its own. */
  const travellingWindows = new Map<string, number>();
  let travelFrame: number | null = null;
  let detachGestures: (() => void) | null = null;
  let store: DesktopStore | null = null;

  // The one menu the desktop ever has open. Which menu it is and what it was opened for is
  // ``openMenu``; opening, placing, dismissing and closing are the component's, and its
  // ``onClose`` is what keeps the two from drifting apart.
  const menu = createMenu({
    placement: "below",
    role: "menu",
    // A menu of verbs never goes under a card's worth of width. The size menu is not one: its
    // content is a grid of tiles, and a floor wider than the grid would only pad it on one side.
    get minWidth(): number | undefined {
      return openMenu?.kind === "size" ? undefined : MENU_MIN_WIDTH;
    },
    // The size menu hangs off an icon a fraction of its own width, so lining their left edges up
    // would point it at the button's corner; centred, it reads as belonging to the whole button.
    // Every other menu is opened from a row or a press, which has a left edge worth aligning to.
    get align(): MenuAlign | undefined {
      return openMenu?.kind === "size" ? "center" : undefined;
    },
    // The marker class each menu is known by. Read off the open menu on every render, so one
    // component can wear all five names.
    get extraClass(): string | undefined {
      return openMenu === null ? undefined : `${openMenu.kind}-menu`;
    },
    onClose: () => {
      openMenu = null;
    },
  });

  /** Whether what is open shields the windows' pages. A menu that lays a sheet does: the press
   *  that dismisses it must not also land on the page under it. The size menu lays none, by
   *  design, so a shield over the pages would swallow the press that its own dismissal needs. */
  function isShieldingOverlayOpen(): boolean {
    return (openMenu !== null && openMenu.kind !== "size") || store?.isLauncherOpen() === true;
  }

  /** Show ``next``'s menu against ``anchor``. A shortcut's menu selects the shortcut on the way:
   *  the selection box is the only thing that says which icon the verbs are about, and a right
   *  click (or a long press) reaches the menu without ever passing through a click that selects. */
  function openMenuAt(next: OpenMenu, anchor: MenuAnchor): void {
    if (next.kind === "shortcut") {
      selectedShortcutKey = shortcutKey(next.shortcut.target.app, next.shortcut.target.launch);
    }
    openMenu = next;
    menu.open(anchor);
  }

  function closeLauncher(): void {
    store?.closeLauncher();
  }

  const onDocumentKeyDown = (event: KeyboardEvent): void => {
    if (event.key !== "Escape") return;
    // One layer per Escape: a dialog (the settings dialog, the avatar chooser) takes it through the
    // Modal's own listener, an open menu through the menu's own, and the layer under it stays.
    if (document.querySelector('.modal-overlay, [data-menu-part="menu"]') !== null) return;
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

  /** Paint a window as the store now has it, straight onto the DOM: its rectangle onto its element, its
   *  page over the content box that just moved, and the snap preview shown or hidden. Per pointer move
   *  of a drag or resize, with no redraw (one per move would re-render the whole desktop and reposition
   *  every page), and once more when the gesture ends or is cancelled: a redraw diffs against the last
   *  render rather than the DOM and writes nothing it finds equal, so the DOM must already be at what
   *  the render answers, which the store's ``windowRect`` and ``snapPreviewRect`` are at every point. */
  /** The chrome of one window as the backdrop holds it now, else null (its window is closed, or its desktop
   *  is no longer the one on screen). */
  function windowElement(windowId: string): HTMLElement | null {
    return backdropArea?.querySelector<HTMLElement>(`[${WINDOW_ID_ATTRIBUTE}="${CSS.escape(windowId)}"]`) ?? null;
  }

  function paintWindow(current: DesktopStore, windowId: string): void {
    const area = backdropArea;
    if (area === null) return;
    const element = windowElement(windowId);
    if (element !== null) applyRectStyle(element, current.windowRect(windowId));
    pages?.placePage(windowId);
    const preview = area.querySelector<HTMLElement>(`[${SNAP_PREVIEW_ATTRIBUTE}]`);
    if (preview !== null) applySnapPreviewStyle(preview, current.snapPreviewRect());
  }

  /** The window a transition belongs to, else null: the chrome's root is the only element whose
   *  travel moves the window, so a transition on something laid out inside it (a control taking
   *  its hover colour) must not drive the page. */
  function travellingWindowId(event: Event): string | null {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return null;
    return target.getAttribute(WINDOW_ID_ATTRIBUTE);
  }

  /** Lay each travelling window's page over the content box as it stands this frame.
   *
   *  A page is positioned by measuring the chrome it sits in rather than by being handed a
   *  rectangle, so it cannot carry the same transition: it would have nothing to transition
   *  towards, and would sit at the old rectangle until something measured the chrome again.
   *  Re-measuring per frame is the placement a drag already does, driven by the transition
   *  instead of by the pointer. */
  function followTravellingWindows(): void {
    for (const windowId of [...travellingWindows.keys()]) {
      // A window that leaves the desktop mid-travel (closed, or its desktop swapped for another) has its
      // transitions cancelled on a chrome already out of the document, where the event never reaches the
      // backdrop that listens for it: the frame that cannot find the chrome is what ends its travel.
      if (windowElement(windowId) === null) travellingWindows.delete(windowId);
      else pages?.placePage(windowId);
    }
    travelFrame = travellingWindows.size > 0 ? requestAnimationFrame(followTravellingWindows) : null;
  }

  function onWindowTravelStart(event: Event): void {
    const windowId = travellingWindowId(event);
    if (windowId === null) return;
    travellingWindows.set(windowId, (travellingWindows.get(windowId) ?? 0) + 1);
    if (travelFrame === null) travelFrame = requestAnimationFrame(followTravellingWindows);
  }

  function onWindowTravelEnd(event: Event): void {
    const windowId = travellingWindowId(event);
    if (windowId === null) return;
    const stillRunning = (travellingWindows.get(windowId) ?? 1) - 1;
    if (stillRunning > 0) {
      travellingWindows.set(windowId, stillRunning);
      return;
    }
    travellingWindows.delete(windowId);
    // Where the chrome landed, not the last value a frame happened to catch on the way.
    pages?.placePage(windowId);
  }

  /** Paint a floating entry as the store now has it, straight onto its box: the per-move step of its drag, and
   *  once more when the drag ends or is cancelled, for the same reason ``paintWindow`` exists. */
  function paintFloatingEntry(current: DesktopStore, app: string): void {
    const element = backdropArea?.querySelector<HTMLElement>(
      `[${PINNED_ENTRY_ATTRIBUTE}="${CSS.escape(app)}"][data-entry-mode="floating"]`,
    );
    if (element !== undefined && element !== null) applyRectStyle(element, current.floatingEntryRectOf(app));
  }

  /** The gesture source measures points against ``root`` (the whole layout, so the taskbar's long presses
   *  count too); the store wants the backdrop's pixels, which differ by whatever sits above the backdrop. */
  function gestureListener(current: DesktopStore, root: HTMLElement): GestureListener {
    const backdropOrigin = (): DOMRect => (backdropArea ?? root).getBoundingClientRect();
    const toBackdrop = (point: PixelPoint): PixelPoint => {
      const rootBox = root.getBoundingClientRect();
      const origin = backdropOrigin();
      return { x: point.x - (origin.left - rootBox.left), y: point.y - (origin.top - rootBox.top) };
    };
    const grabOffsetInside = (element: HTMLElement, press: PixelPoint): PixelPoint => {
      const box = element.getBoundingClientRect();
      const origin = backdropOrigin();
      return { x: press.x - (box.left - origin.left), y: press.y - (box.top - origin.top) };
    };
    /** The pinned window of ``app`` on the active desktop, whose entry a floating-entry press names. */
    const pinnedWindowIdOf = (app: string): string | null => pinnedWindowOf(current.getState(), app)?.id ?? null;
    return {
      thresholdPx: () => current.getMetrics().dragThreshold,
      isDraggable: (binding) => {
        if (binding.kind === "shortcut") return true;
        if (binding.kind === "taskbar-entry") return false;
        return !current.getState().modes.isCompact;
      },
      // Inert for the whole press: the pixels before the threshold are spent beside the handle, often
      // over a neighbouring page, and a move the root cannot see is a move the threshold never counts.
      onPressStart: () => {
        pages?.setGestureActive(true);
        root.setAttribute(WINDOW_MOTION_ATTRIBUTE, "off");
      },
      onPressEnd: () => {
        pages?.setGestureActive(false);
        root.removeAttribute(WINDOW_MOTION_ATTRIBUTE);
      },
      onBegin: (binding, rootPoint, rootPress) => {
        const point = toBackdrop(rootPoint);
        switch (binding.kind) {
          case "window-move":
            root.setAttribute(WINDOW_DRAGGING_ATTRIBUTE, "");
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
              grabOffsetInside(binding.element, toBackdrop(rootPress)),
            );
            return;
          case "floating-entry":
            current.beginFloatingEntryDrag(
              binding.app,
              point,
              grabOffsetInside(binding.element, toBackdrop(rootPress)),
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
            paintWindow(current, binding.windowId);
            return;
          case "window-resize":
            current.updateWindowResize(delta);
            paintWindow(current, binding.windowId);
            return;
          case "shortcut":
            current.updateShortcutDrag(point);
            return;
          case "floating-entry":
            current.updateFloatingEntryDrag(point);
            paintFloatingEntry(current, binding.app);
            return;
          case "taskbar-entry":
            return;
        }
      },
      onEnd: (binding, rootPoint, delta) => {
        const point = toBackdrop(rootPoint);
        switch (binding.kind) {
          case "window-move":
            root.removeAttribute(WINDOW_DRAGGING_ATTRIBUTE);
            current.endWindowMove(point);
            paintWindow(current, binding.windowId);
            break;
          case "window-resize":
            current.endWindowResize(delta);
            paintWindow(current, binding.windowId);
            break;
          case "shortcut":
            current.endShortcutDrag(point);
            break;
          case "floating-entry":
            current.endFloatingEntryDrag(point);
            paintFloatingEntry(current, binding.app);
            break;
          case "taskbar-entry":
            break;
        }
      },
      onCancel: (binding) => {
        root.removeAttribute(WINDOW_DRAGGING_ATTRIBUTE);
        current.cancelGesture();
        if (binding.kind === "window-move" || binding.kind === "window-resize") paintWindow(current, binding.windowId);
        if (binding.kind === "floating-entry") paintFloatingEntry(current, binding.app);
      },
      onLongPress: (binding, client) => {
        const anchor = anchorForPoint(client.x, client.y);
        switch (binding.kind) {
          case "window-move":
          case "window-resize":
            openMenuAt({ kind: "window", windowId: binding.windowId }, anchor);
            break;
          case "shortcut": {
            const shortcut = activeDesktop(current.getState())?.shortcuts.find(
              (candidate) => candidate.target.app === binding.app && candidate.target.launch === binding.launch,
            );
            if (shortcut !== undefined) openMenuAt({ kind: "shortcut", shortcut }, anchor);
            break;
          }
          case "taskbar-entry":
            openMenuAt({ kind: "entry", windowId: binding.windowId }, anchor);
            break;
          case "floating-entry": {
            const windowId = pinnedWindowIdOf(binding.app);
            if (windowId !== null) openMenuAt({ kind: "entry", windowId }, anchor);
            break;
          }
        }
        m.redraw();
      },
    };
  }

  /** The zone grid's actions for a window, or null where there is nothing to choose: compact mode,
   *  where every window renders maximized, and a window that has since been closed -- a hover menu
   *  outlives its trigger, and placing a window that is gone writes a placement nothing owns. */
  function sizeActionsOf(current: DesktopStore, windowId: string): WindowSizeActions | null {
    const state = current.getState();
    if (state.modes.isCompact) return null;
    if (!activeDesktop(state)?.windows.some((candidate) => candidate.id === windowId)) return null;
    return {
      setState: (state) => current.setWindowState(windowId, state),
      setFrame: (frame) => current.setWindowFrame(windowId, frame),
    };
  }

  /** The maximize control's own menu: the zone grid under its own heading. */
  function rowsOfSizeMenu(current: DesktopStore, windowId: string): MenuRow[] | null {
    const actions = sizeActionsOf(current, windowId);
    if (actions === null) return null;
    return [windowSizeRow(actions, () => menu.close())];
  }

  /** Spread onto a window's maximize control: resting on it opens that window's size menu. */
  function sizeMenuTrigger(windowId: string): m.Attributes {
    return menu.hoverTriggerAttrs(() => {
      openMenu = { kind: "size", windowId };
    });
  }

  function rowsOfWindowMenu(current: DesktopStore, windowId: string): MenuRow[] | null {
    const state = current.getState();
    const window = activeDesktop(state)?.windows.find((candidate) => candidate.id === windowId);
    if (window === undefined) return null;
    const app = appByName(state, window.app);
    return windowMenuRows(app, {
      size: sizeActionsOf(current, windowId),
      onSized: () => menu.close(),
      share:
        app === undefined || app.critical
          ? null
          : () => sendToEmbedder(OPEN_SHARE_SETTINGS, { serviceName: app.name }),
      setAppLifecycle:
        app !== undefined && current.canStopApp(app)
          ? (action) => void current.setAppLifecycle(app.name, action)
          : null,
      close: () => void current.closeOrMinimizeWindow(windowId),
    });
  }

  function rowsOfEntryMenu(current: DesktopStore, windowId: string): MenuRow[] | null {
    const state = current.getState();
    const window = activeDesktop(state)?.windows.find((candidate) => candidate.id === windowId);
    if (window === undefined) return null;
    const placement = placementOf(state.layout, windowId);
    const look = entryLook(state, window, appByName(state, window.app));
    return taskbarEntryMenuRows(
      {
        isMinimized: placement.is_minimized,
        isMaximized: placement.state === "MAXIMIZED",
        restore: () => current.restoreWindow(windowId),
        minimize: () => current.minimizeWindow(windowId),
        maximize: () => current.setWindowState(windowId, "MAXIMIZED"),
        unmaximize: () => current.toggleMaximized(windowId),
        close: () => void current.closeOrMinimizeWindow(windowId),
        presentation:
          look === null
            ? null
            : {
                look,
                setMode: (mode) => void current.setEntryMode(window.app, mode),
                setStyle: (style) => void current.setEntryStyle(window.app, style),
                changeAvatar: () => openAvatarChooser(current),
              },
      },
      state.modes.isCompact,
    );
  }

  function rowsOfShortcutMenu(current: DesktopStore, opened: DesktopShortcut): MenuRow[] | null {
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
    const rows: MenuRow[] = [
      { kind: "action", key: "open", label: "Open", onSelect: () => void current.runShortcut(shortcut) },
    ];
    if (shortcut.mode === "focus" && launchPath !== null) {
      rows.push({
        kind: "action",
        key: "open-new",
        label: launchPath.label,
        onSelect: () => void current.runLaunch(shortcut.target.app, shortcut.target.launch, "new"),
      });
    } else if (shortcut.mode === "new" && app !== undefined) {
      rows.push({
        kind: "action",
        key: "focus-last",
        label: `Focus last ${app.display_name}`,
        isDisabled: recent === null,
        onSelect: () => {
          if (recent !== null) current.raiseWindow(recent.id);
        },
      });
    }
    rows.push(
      {
        kind: "action",
        key: "change-mode",
        label: otherMode === "new" ? "Always open a new window" : "Focus the last window instead",
        onSelect: () => void current.setShortcut(desktop.id, { ...shortcut, mode: otherMode }),
      },
      { kind: "divider" },
      {
        kind: "action",
        key: "remove",
        label: "Remove",
        icon: "trash",
        tone: "danger",
        onSelect: () => void current.removeShortcut(shortcut.target.app, shortcut.target.launch),
      },
    );
    return rows;
  }

  function rowsOfDesktopsMenu(current: DesktopStore): MenuRow[] {
    const state = current.getState();
    const active = state.activeDesktopId;
    return [
      {
        kind: "action",
        key: "new-desktop",
        label: "New desktop",
        onSelect: () => {
          const glyphIndex = nextGlyphIndex(
            state.desktops.map((desktop) => desktop.glyph),
            SQUIGGLE_GLYPHS.length,
          );
          void current.createDesktop(nextDesktopName(state.desktops), SQUIGGLE_GLYPHS[glyphIndex].color, glyphIndex);
        },
      },
      { kind: "divider" },
      {
        kind: "action",
        key: "settings",
        label: "Desktop settings...",
        isDisabled: active === null,
        onSelect: () => openSettings(active, false),
      },
      {
        kind: "action",
        key: "delete",
        label: "Delete desktop...",
        tone: "danger",
        isDisabled: active === null,
        onSelect: () => openSettings(active, true),
      },
    ];
  }

  function rowsOfDesktopMenu(current: DesktopStore, desktopId: string): MenuRow[] {
    return [
      {
        kind: "action",
        key: "switch",
        label: "Switch to this desktop",
        onSelect: () => void current.switchDesktop(desktopId),
      },
      { kind: "action", key: "settings", label: "Settings...", onSelect: () => openSettings(desktopId, false) },
      { kind: "divider" },
      {
        kind: "action",
        key: "delete",
        label: "Delete...",
        tone: "danger",
        onSelect: () => openSettings(desktopId, true),
      },
    ];
  }

  /** The rows of whichever menu is open, or null once what it was opened for has gone. */
  function rowsOfOpenMenu(current: DesktopStore, open: OpenMenu): MenuRow[] | null {
    switch (open.kind) {
      case "window":
        return rowsOfWindowMenu(current, open.windowId);
      case "size":
        return rowsOfSizeMenu(current, open.windowId);
      case "entry":
        return rowsOfEntryMenu(current, open.windowId);
      case "shortcut":
        return rowsOfShortcutMenu(current, open.shortcut);
      case "desktops":
        return rowsOfDesktopsMenu(current);
      case "desktop":
        return rowsOfDesktopMenu(current, open.desktopId);
    }
  }

  function openSettings(desktopId: string | null, isDeleting: boolean): void {
    if (desktopId === null) return;
    settingsDialog = { desktopId, isDeleting };
    // Read on every open: a file dropped into the wallpapers directory shows up, and a read that failed
    // last time is tried again; the last list stays on screen meanwhile.
    void fetchWallpapers()
      .then((listed) => {
        wallpapers = listed;
      })
      .catch((error: unknown) => {
        console.warn("[si] could not list the wallpapers", error);
        wallpapers ??= [];
      })
      .finally(() => m.redraw());
  }

  function openAvatarChooser(current: DesktopStore): void {
    const opened: AvatarChooserState = { designs: null, loadError: null };
    avatarChooser = opened;
    // The dialog can be closed (or opened again) while the catalog loads; the answer is only this opening's.
    void current
      .fetchAvatarDesigns()
      .then((designs) => {
        if (avatarChooser === opened) avatarChooser = { designs, loadError: null };
      })
      .catch((error: unknown) => {
        console.warn("[si] could not list the avatar designs", error);
        if (avatarChooser === opened) {
          avatarChooser = { designs: null, loadError: `Could not list the designs: ${(error as Error).message}` };
        }
      })
      .finally(() => m.redraw());
  }

  function avatarChooserView(current: DesktopStore, chooser: AvatarChooserState): m.Children {
    const state = current.getState();
    const target = draftTargetOf(state);
    return m(AvatarChooserDialog, {
      designs: chooser.designs,
      loadError: chooser.loadError,
      selected: state.avatar.design,
      onSelect: (design) => void current.selectAvatar(design),
      onDesignOwn:
        target === null
          ? null
          : () => {
              avatarChooser = null;
              void current.draftIntoPinnedWindow(AVATAR_DESIGN_PROMPT);
            },
      onClose: () => {
        avatarChooser = null;
      },
    });
  }

  function settingsDialogView(current: DesktopStore, dialog: SettingsDialogState): m.Children {
    const desktop = desktopById(current.getState(), dialog.desktopId);
    if (desktop === null) return null;
    return m(DesktopSettingsDialog, {
      desktop,
      wallpapers,
      isDeleting: dialog.isDeleting,
      onSave: async (name, color, glyph, wallpaper) => {
        await current.updateDesktopSettings(desktop.id, name, color, glyph);
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

  function replacedDesktopNotice(current: DesktopStore): m.Children {
    const replaced = current.getReplacedDesktop();
    if (replaced === null) return null;
    return m(ReplacedDesktopNotice, {
      replacedDesktopName: replaced.replacedName,
      seededDesktopName: replaced.seededName,
      onDismiss: () => current.dismissReplacedDesktopNotice(),
    });
  }

  function launcherMenu(current: DesktopStore): LauncherMenuRows {
    return launcherRowsOf(current.getState(), launcherQuery);
  }

  /** The row Enter runs: where the arrows or a hover left the highlight while that row is still shown and
   *  enabled, else the default of plan section 3.5. */
  function launcherHighlightIndex(rows: readonly LauncherRow[]): number {
    const moved = launcherHighlight;
    if (moved !== null && moved >= 0 && moved < rows.length && isRowEnabled(rows[moved])) return moved;
    return defaultHighlightIndex(rows);
  }

  /** Run a row (plan section 4.3): the menu closes and the field is cleared first, whatever the row does. */
  function runLauncherRow(current: DesktopStore, row: LauncherRow): void {
    closeLauncher();
    launcherQuery = "";
    launcherHighlight = null;
    switch (row.kind) {
      case "launch":
        void current.runLaunchRow(row.app.name, row.launchPath.id);
        return;
      case "window":
        if (row.desktopId !== current.getState().activeDesktopId) {
          void current.switchDesktop(row.desktopId).then(() => current.restoreWindow(row.window.id));
        } else {
          current.restoreWindow(row.window.id);
        }
        return;
      case "text":
        void current.runFreeText(row.app.name, row.launchPath.id, row.text);
        return;
    }
  }

  function runHighlightedRow(current: DesktopStore): void {
    const { rows } = launcherMenu(current);
    const index = launcherHighlightIndex(rows);
    if (index >= 0) runLauncherRow(current, rows[index]);
  }

  function runSecondaryRow(current: DesktopStore): void {
    const row = secondaryTextRow(launcherMenu(current).rows);
    if (row !== null) runLauncherRow(current, row);
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
        void current.closeOrMinimizeWindow(windowId);
        return;
      case "refresh":
        current.refreshWindow(windowId);
        return;
      case "menu":
        // A press while this menu is up lands on the menu's own sheet and closes it there, so the
        // click that reaches the kebab is almost always the opening one; the toggle stands for the
        // keyboard, which has no press to swallow.
        if (openMenu?.kind === "window" && openMenu.windowId === windowId) menu.close();
        else openMenuAt({ kind: "window", windowId }, anchorForEvent(event));
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
    },
    onupdate() {
      pages?.reconcile();
    },
    onremove() {
      document.removeEventListener("keydown", onDocumentKeyDown);
      document.removeEventListener("pointerdown", onDocumentPointerDown, true);
      // A menu still open here would keep its own window listeners for good.
      menu.dispose();
      detachGestures?.();
      resizeObserver?.disconnect();
      if (travelFrame !== null) cancelAnimationFrame(travelFrame);
    },
    view(vnode) {
      const current = vnode.attrs.store;
      store = current;
      const state = current.getState();
      const desktop: Desktop | null = activeDesktop(state);
      const placements = activePlacements(state);
      const focused = activeFocusedWindowId(state);
      const isLauncherOpen = current.isLauncherOpen();
      const launcher = launcherMenu(current);
      // A pinned entry answers the same way in the bar and afloat.
      const onEntryClick = (windowId: string): void => current.toggleTaskbarEntry(windowId);
      const onEntryContextMenu = (windowId: string, x: number, y: number): void => {
        openMenuAt({ kind: "entry", windowId }, anchorForPoint(x, y));
      };
      const menuRows = openMenu === null ? null : rowsOfOpenMenu(current, openMenu);
      // The wallpaper is painted here rather than on the backdrop so it spans the whole viewport:
      // the taskbar's translucent surface then has the desktop behind it to blur, and the backdrop
      // stays the viewport less the taskbar height that the geometry rules measure.
      const wallpaperStyle =
        desktop?.wallpaper == null ? {} : { backgroundImage: `url("${wallpaperImageUrl(desktop.wallpaper)}")` };
      return m(
        "div",
        {
          class: "app-layout flex h-screen flex-col bg-page bg-cover bg-center bg-(image:--desk-default-wallpaper)",
          style: wallpaperStyle,
        },
        [
          m(UpdateStalenessBanner),
          m(UpdateNoticeBanner, { store: current }),
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
                // The window roots are rendered and re-rendered under here, so the travel is bound
                // once to the backdrop the events bubble to rather than per window.
                backdropArea.addEventListener("transitionrun", onWindowTravelStart);
                backdropArea.addEventListener("transitionend", onWindowTravelEnd);
                backdropArea.addEventListener("transitioncancel", onWindowTravelEnd);
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
                    floatingEntries: floatingEntries(state),
                    openEntryMenuWindowId: openMenu?.kind === "entry" ? openMenu.windowId : null,
                    sizeMenuTrigger,
                    onEntryClick,
                    onEntryContextMenu,
                    isOverlayOpen: isShieldingOverlayOpen(),
                    onSelectShortcut: (key) => {
                      selectedShortcutKey = key;
                    },
                    onRunShortcut: (shortcut) => void current.runShortcut(shortcut),
                    onShortcutContextMenu: (shortcut, point) => {
                      openMenuAt({ kind: "shortcut", shortcut }, anchorForPoint(point.x, point.y));
                    },
                    onWindowControl: (windowId, control, event) => onWindowControl(current, windowId, control, event),
                    onPagesHostCreated: (host) => {
                      pages = new LivePagesLayer(host, current, {
                        host: vnode.attrs.host,
                        protocol: vnode.attrs.protocol,
                      });
                      pages.start();
                      // The render that made the host is over (the window chrome is in the DOM), and when every
                      // load had already landed no further redraw follows it: the pages are placed now.
                      pages.reconcile();
                    },
                  }),
              isLauncherOpen
                ? m(LauncherMenu, {
                    menu: launcher,
                    highlightIndex: launcherHighlightIndex(launcher.rows),
                    isCompact: state.modes.isCompact,
                    isApplePlatform: isApplePlatform(),
                    bottomOffsetPx: launcherFieldRise,
                    onRun: (row) => runLauncherRow(current, row),
                    onHighlight: (index) => {
                      launcherHighlight = index;
                    },
                  })
                : null,
            ],
          ),
          m(Taskbar, {
            entries: barEntries(state),
            avatar: state.avatar,
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
                launcherHighlight = null;
              },
              onMoveHighlight: (delta) => {
                const { rows } = launcherMenu(current);
                launcherHighlight = moveHighlight(rows, launcherHighlightIndex(rows), delta);
              },
              onRunHighlight: () => runHighlightedRow(current),
              onRunSecondary: () => runSecondaryRow(current),
              onRise: (rise) => {
                launcherFieldRise = rise;
                m.redraw();
              },
            },
            tray: {
              desktops: state.desktops,
              activeDesktopId: state.activeDesktopId,
              isDesktopsMenuOpen: openMenu?.kind === "desktops",
              onSwitchDesktop: (desktopId) => void current.switchDesktop(desktopId),
              onOpenDesktopsMenu: (event) => {
                if (openMenu?.kind === "desktops") menu.close();
                else openMenuAt({ kind: "desktops" }, anchorForEvent(event));
              },
              onDesktopContextMenu: (desktopId, x, y) => {
                openMenuAt({ kind: "desktop", desktopId }, anchorForPoint(x, y));
              },
            },
            onEntryClick,
            onEntryContextMenu,
          }),
          menuRows === null ? null : menu.view(menuRows),
          settingsDialog === null ? null : settingsDialogView(current, settingsDialog),
          replacedDesktopNotice(current),
          avatarChooser === null ? null : avatarChooserView(current, avatarChooser),
        ],
      );
    },
  };
}
