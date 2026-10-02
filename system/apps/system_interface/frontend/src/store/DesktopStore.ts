/**
 * ``DesktopStore``: the one class with mutable fields (desktop-interface plan section 6.2). It
 * holds this client's state (the record the reducers step), the theme metrics and the backdrop
 * size the geometry needs, the gesture in progress, and whether the launcher is open; applies
 * the reducers; schedules redraws; saves the layout with a debounce, a save id, and the stamp it
 * was based on (a stale save is refused with 409 and the layout refetched); and subscribes to
 * the socket. Everything that reads or writes the shell goes through here, the phone layout's showing of one
 * window at a time included (plan-phone-interface.md): a phone never writes a placement, lands where it
 * left off, and opens every window on the first desktop, out of every other client's sight.
 */

import type {
  LaunchOutcome,
  LaunchRequest,
  LaunchTarget,
  PlacementsSaveRequest,
  WindowOpenOutcome,
  WindowOpenRequest,
} from "../model/api";
import { StalePlacementsSaveError } from "../model/api";
import {
  NO_DRAFT_APP_REASON,
  NO_TEXT_APP_REASON,
  appShortcutOf,
  draftRowsOf,
  freeTextParams,
  freeTextRowsOf,
  launchPathOf,
  launchRowKindOf,
  textRowDisabledReason,
} from "../model/launch";
import { applyPresence } from "../model/Presence";
import type {
  AppRecord,
  AvatarCatalog,
  AvatarDesign,
  ClientArrival,
  ClientRecord,
  Desktop,
  DesktopShortcut,
  EntryMode,
  EntryPresentation,
  FloatingPosition,
  Frame,
  GridCell,
  IfPresent,
  Inventory,
  LaunchPath,
  Layout,
  PinStyle,
  Placement,
  PresentUser,
  ShortcutMode,
  Wallpaper,
  WindowRecord,
  WindowState,
} from "../model/records";
import { isSameCell, isSameWindowPaths, shortcutKey } from "../model/records";
import { ToastQueue } from "../model/Toasts";
import { OwnIdMinter, REPORT_ID_PREFIX, SAVE_ID_PREFIX } from "../model/ownIds";
import { isPreviewShell } from "../model/PreviewShell";
import { noticeFromWire } from "../model/UpdateNotice";
import type { DeepLink } from "../model/deepLinks";
import {
  MAXIMIZED_FRAME,
  fitFrameToBackdrop,
  frameForState,
  frameFromPixels,
  frameToPixels,
  movedRect,
  resizedRect,
  snapZoneForRelease,
  unsnapFrame,
} from "../geometry/frames";
import type { PixelPoint, PixelRect, PixelSize, ResizeEdge } from "../geometry/frames";
import { defaultFloatingPosition, floatingEntryRect, floatingPositionFromPixels } from "../geometry/floating";
import {
  cellAtPoint,
  cellRect,
  gridDimensions,
  placeShortcuts,
  placedShortcutKey,
  withRoomMadeFor,
} from "../geometry/grid";
import type { GridDimensions, PlacedShortcut } from "../geometry/grid";
import { placementOf } from "../geometry/stack";
import {
  activeDesktop,
  activeFocusedWindowId,
  appByName,
  desktopById,
  detachedWindowsOf,
  draftTargetOf,
  effectiveWindow,
  effectiveWindowTitle,
  entryLook,
  findWindow,
  initialDesktopState,
  isAppStoppable,
  isEmbedderMessageHandled,
  isLayoutDirty,
  openableApps,
  pinnedWindowOf,
  reduceDesktopState,
  shownHistoryEntry,
  withShownRecorded,
} from "../reducers/desktopState";
import type {
  DesktopEvent,
  DesktopState,
  DetachedWindowReport,
  PhoneSheet,
  PhoneShown,
} from "../reducers/desktopState";
import { focusTargetOf, isShownWindowGone, phoneLanding, shownWindowOf } from "../reducers/phone";
import {
  STILL_CONNECTING_NOTICE,
  cellForAddedShortcut,
  findShortcut,
  isShortcutOf,
  resolveLaunchRun,
} from "../reducers/shortcuts";
import type { ThemeMetrics, RenderModes } from "../theme/metrics";
import type {
  ActiveDesktopChangedEvent,
  ClientEntriesChangedEvent,
  DesktopSocket,
  LayoutOpEvent,
  PlacementsUpdatedEvent,
} from "./socket";

// A gesture's save lands shortly after it ends; the shell edits the saved layout for agent ops, and
// an op that follows a gesture has to see the gesture in the file.
const SAVE_DEBOUNCE_MS = 300;

/** Why a window switches desktops: the user chose one ("user"), the bootstrap lands it ("landing"), or it follows the
 *  client's stored desktop, pushed or re-read ("follow"). The first two move the client; a follow moves nothing. */
export type SwitchCause = "user" | "landing" | "follow";

// How long a solo shell whose first layout does not say its window is out waits for the desktop's word before
// writing the detach itself: the main window's shell writes it as the window leaves, and that save is on its
// way while the solo shell boots, so a first load that beats it is followed by the broadcast within this.
const SOLO_HEAL_GRACE_MS = 1000;

/** The routes the store calls, injectable so the store is tested against a fake shell. */
export interface DesktopApi {
  /** The desktops, the apps, and the clients in one read (contracts.md section 5.5): what a page boots from. */
  fetchInventory(): Promise<Inventory>;
  createDesktop(name: string, color: string, glyph: number): Promise<Desktop>;
  updateDesktopSettings(desktopId: string, name: string, color: string, glyph: number): Promise<Desktop>;
  setDesktopWallpaper(desktopId: string, wallpaper: Wallpaper | null): Promise<Desktop>;
  deleteDesktop(desktopId: string): Promise<string>;
  setDesktopShortcut(desktopId: string, shortcut: DesktopShortcut): Promise<Desktop>;
  moveDesktopShortcut(desktopId: string, app: string, launch: string, cell: GridCell): Promise<Desktop>;
  removeDesktopShortcut(desktopId: string, app: string, launch: string): Promise<Desktop>;
  openWindow(desktopId: string, request: WindowOpenRequest): Promise<WindowOpenOutcome>;
  /** Run a launch path (post-launch-paths plan section 5.3): the shell resolves the page and opens or navigates. */
  launch(desktopId: string, request: LaunchRequest): Promise<LaunchOutcome>;
  closeWindow(desktopId: string, windowId: string): Promise<void>;
  reportWindowLocation(
    desktopId: string,
    windowId: string,
    clientId: string,
    path: string,
    title: string,
  ): Promise<WindowRecord>;
  fetchPlacements(desktopId: string, clientId: string): Promise<Layout>;
  savePlacements(desktopId: string, request: PlacementsSaveRequest): Promise<string | null>;
  arriveClient(clientId: string): Promise<ClientArrival>;
  fetchClients(): Promise<ClientRecord[]>;
  /** Record what this client's phone layout shows: a window, or the home grid for null. */
  recordShown(clientId: string, windowId: string | null): Promise<ClientRecord>;
  quitApp(appName: string): Promise<void>;
  setEntryPresentation(clientId: string, app: string, presentation: EntryPresentation): Promise<ClientRecord>;
  fetchAvatars(): Promise<AvatarCatalog>;
  selectAvatar(design: string): Promise<void>;
  relayEmbedderMessage(type: string, clientId: string, payload: Readonly<Record<string, unknown>>): Promise<void>;
}

/** A message the Imbue Studio chrome sent this page: its type and its own fields. */
export type EmbedderMessage = { readonly type: string } & Readonly<Record<string, unknown>>;

/** What the live-page layer does for the store, registered by that layer (it sits above the store). */
export interface PageDriver {
  /** Reload one window's page. */
  reload(windowId: string): void;
  /** Reload every page of an app. */
  reloadApp(appName: string): void;
  /** Send the page ``shell:close-request`` (the Imbue Studio close chord). */
  requestClose(windowId: string): void;
  /** Whether the window's page declared it owns the close chord (``closeChord: true``). */
  ownsCloseChord(windowId: string): boolean;
}

/** What the shell asks the embedding chrome for when a window is pulled out (the pull-out-window spec). */
export interface PopOutRequest {
  readonly windowId: string;
  readonly title: string;
  /** The window's rendered size in CSS px. */
  readonly width: number;
  readonly height: number;
}

/** A title-bar drag announced to the chrome, which watches the cursor from there (the pull-out-window spec,
 *  section 5.1): the window's rendered size and where inside it the pointer holds it, so the desktop window the
 *  chrome opens once the cursor leaves its window is sized and held the same way. */
export interface WindowDragRequest extends PopOutRequest {
  readonly grabX: number;
  readonly grabY: number;
}

/** A step of a watched drag as the chrome reports it (``minds:tear-out``): the cursor left the chrome's window
 *  by the tear-out distance and a desktop window of the chrome's follows it, came back inside, or the button was
 *  released while out. */
export type TearOutPhase = "out" | "in" | "released";

/** The shell's side of the pull-out conversation with the embedding chrome, injectable so the store is tested
 *  against a recorder. Every call is a no-op without an embedder. */
export interface PopOutBridge {
  requestPopOut(request: PopOutRequest): void;
  beginWindowDrag(request: WindowDragRequest): void;
  endWindowDrag(windowId: string, isDetached: boolean, isCancelled: boolean): void;
  reportDetachedWindows(windows: readonly DetachedWindowReport[]): void;
}

const NULL_POP_OUT_BRIDGE: PopOutBridge = {
  requestPopOut: () => undefined,
  beginWindowDrag: () => undefined,
  endWindowDrag: () => undefined,
  reportDetachedWindows: () => undefined,
};

// The layout verbs that edit a placement, which two shells never apply. A solo shell (the pull-out-window spec,
// section 7.5) is a view of one window, and the desktop's arrangement belongs to the client's main window; its
// two exceptions are its own window's detach (the first load's fallback, when no desktop shell wrote it) and
// reattach (the way back when no desktop shell can take it). The phone layout shows windows without placing them.
const PLACEMENT_EDIT_EVENTS: ReadonlySet<DesktopEvent["type"]> = new Set([
  "window_raised",
  "window_minimized",
  "window_restored",
  "window_state_set",
  "window_frame_set",
  "window_detached",
  "window_reattached",
]);

export interface StoreDependencies {
  readonly clientId: string;
  readonly api: DesktopApi;
  readonly socket: DesktopSocket;
  readonly metrics: ThemeMetrics;
  readonly modes: RenderModes;
  /** Schedule a redraw of the views after a state change. */
  readonly redraw: () => void;
  /** Reload the whole interface (the ``reload_system_interface`` op). */
  readonly reloadInterface: () => void;
  /** The pull-out conversation with the embedder; absent means no embedder. */
  readonly popOut?: PopOutBridge;
  /** The one window this shell shows edge to edge (a pulled-out window's own desktop window), else null. */
  readonly soloWindowId?: string | null;
  /** Whether the chrome reopened the solo window's desktop window (a session restore, a reopen of the app, a
   *  backend retry) rather than opening it for a tear-out just now; absent means a tear-out, as an older chrome
   *  says. */
  readonly isSoloReopened?: boolean;
}

/** A navigation this client asked for on its own page (the chooser's draft), which the live pages honour
 *  even where the page just reported leaving that very path. */
export interface OwnNavigation {
  readonly windowId: string;
  readonly path: string;
}

/** A window move in progress: the rendered rectangle it started from and where it is now. */
export interface MoveGesture {
  readonly kind: "move";
  readonly windowId: string;
  readonly startRect: PixelRect;
  readonly startPointer: PixelPoint;
  readonly currentRect: PixelRect;
  /** The snap zone the pointer is in right now, drawn as the preview; applied on release. */
  readonly zone: WindowState | null;
  /** Whether a snapped or maximized window has been un-snapped by this drag. */
  readonly isUnsnapped: boolean;
  /** Whether the chrome watches this drag (``minds:window-drag-started`` went out): it opens a desktop window of
   *  its own once the cursor leaves its window, and hears when the gesture ends here. */
  readonly isWatched: boolean;
  /** Whether the chrome reported the cursor past its window by the tear-out distance: it is dragging a desktop
   *  window of its own under the cursor, and this one is hidden until the drag comes back inside or ends (the
   *  pull-out-window spec). */
  readonly isTearingOut: boolean;
}

export interface ResizeGesture {
  readonly kind: "resize";
  readonly windowId: string;
  readonly edge: ResizeEdge;
  readonly startRect: PixelRect;
  readonly currentRect: PixelRect;
}

export interface ShortcutGesture {
  readonly kind: "shortcut";
  readonly app: string;
  readonly launch: string;
  /** The cell it was lifted from: the box it is drawn in for the whole drag, which the lift translates
   *  rather than moves, so the icon stays exactly where it was grabbed under the hand. */
  readonly originCell: GridCell;
  /** How far the pointer has travelled since the press: what the lifted icon is translated by. */
  readonly lift: PixelPoint;
  /** The cell the pointer is over: where the icon would land, and what the shortcut in it steps aside from. */
  readonly targetCell: GridCell;
}

/** A floating entry being dragged: its box's top-left corner follows the pointer less the grab offset. */
export interface FloatingEntryGesture {
  readonly kind: "floating-entry";
  readonly app: string;
  readonly grabOffset: PixelPoint;
  readonly currentRect: PixelRect;
}

/** A shortcut and the cell it should take: what an optimistic write of the desktop names, one per shortcut. */
interface ShortcutCellUpdate {
  readonly app: string;
  readonly launch: string;
  readonly cell: GridCell;
}

export type ActiveGesture = MoveGesture | ResizeGesture | ShortcutGesture | FloatingEntryGesture;

/** A watched drag this shell ended on its own release, whose last word from the chrome may still be on its way. */
interface ReleasedWatchedDrag {
  readonly windowId: string;
  readonly isDetached: boolean;
  /** The window's placement as the drag began, which a late pull-out puts back before detaching it. */
  readonly placementAtStart: Placement;
}

type Listener = () => void;

export class DesktopStore {
  private state: DesktopState;
  private metrics: ThemeMetrics;
  private backdrop: PixelSize = { width: 0, height: 0 };
  private gesture: ActiveGesture | null = null;
  private releasedWatchedDrag: ReleasedWatchedDrag | null = null;
  private isLauncherOpenNow = false;
  // Set when the shell had to seed a fresh desktop for this user at arrival; the notice shows once.
  private replacedDesktop: ReplacedDesktop | null = null;
  private readonly listeners = new Set<Listener>();
  private readonly saveIds = new OwnIdMinter(SAVE_ID_PREFIX);
  private readonly reportIds = new OwnIdMinter(REPORT_ID_PREFIX);
  // The newest revision of the client's stored desktop this window has heard of, pushed or read: news of an older
  // one was overtaken by a later move on its way here.
  private desktopRevisionHeard = 0;
  private saveTimer: ReturnType<typeof setTimeout> | null = null;
  private saveInFlight: Promise<void> | null = null;
  private layoutFetchSequence = 0;
  private pageDriver: PageDriver | null = null;
  private hasSocketConnected = false;
  // The path each window's page reported last, so a report answered out of order is not applied.
  private readonly latestReportedPaths = new Map<string, string>();
  /** The one navigation this client asked for itself and has not yet followed (``navigateOwnWindow``). */
  private ownNavigation: OwnNavigation | null = null;
  // Bumped by every desktops record the shell hands over (the bootstrap's read, each broadcast, its answer to a
  // linked window's navigate this window landed), and not by a local edit: the live pages follow their windows'
  // stored paths after the shell speaks.
  private desktopsRevision = 0;
  // Bumped by every layout the shell hands over: an independent window's stored path arrives with the layout.
  private layoutLoadsRevision = 0;
  // Bumped by every push of the workspace's selection and of this client's entries: a read issued before a
  // push answers older than the push, and must not overwrite it.
  private avatarSelectionPushes = 0;
  private entryPushes = 0;
  // Resolved by the first app list to land: the bootstrap's inventory read, or the socket's ``apps_updated`` when
  // the socket is quicker; a deep link's open or launch waits for it.
  private readonly appsLoaded: Promise<void>;
  private markAppsLoaded: () => void = () => undefined;
  private readonly popOut: PopOutBridge;
  private readonly soloWindowId: string | null;
  /** Whether the embedder can pull a window out; off until it says so (an older chrome, a plain browser). */
  private canPopOut = false;
  /** The detached set as last reported to the embedder, serialized, so a redraw reports nothing new. */
  private lastReportedDetached: string | null = null;
  /** Solo mode's first layout load is still owed: the one load that may re-detach the solo window. */
  private isSoloFirstLayoutPending: boolean;
  /** The grace a solo shell gives the desktop's detach save before writing the detach itself. */
  private soloHealTimer: ReturnType<typeof setTimeout> | null = null;
  /** A solo shell's reattach is on its way to the shell: the detached-set report waits for it to land, since
   *  the chrome closes this window on a report without its window, which would abort the save. */
  private isReportHeldForSave = false;
  /** A window an agent op showed this phone before the desktops update that brings it landed. */
  private pendingPhoneShowId: string | null = null;
  /** Whether the page was out of sight since the socket last connected: a reconnect then reloads the phone's page,
   *  whose own connection most likely went down with the shell's. */
  private isHiddenSinceConnect = false;
  /** The notes a refusal leaves on screen (plan-phone-interface.md's toasts). */
  readonly toasts: ToastQueue;

  constructor(private readonly deps: StoreDependencies) {
    this.state = initialDesktopState(deps.clientId, deps.modes);
    this.metrics = deps.metrics;
    this.popOut = deps.popOut ?? NULL_POP_OUT_BRIDGE;
    this.soloWindowId = deps.soloWindowId ?? null;
    this.isSoloFirstLayoutPending = this.soloWindowId !== null;
    this.appsLoaded = new Promise((resolve) => {
      this.markAppsLoaded = resolve;
    });
    this.toasts = new ToastQueue(() => this.notifyListeners());
  }

  /** Tell the user about a refusal. */
  private toast(message: string): void {
    this.toasts.show(message);
  }

  /** The one window this shell shows alone, or null for the whole desktop. */
  getSoloWindowId(): string | null {
    return this.soloWindowId;
  }

  /** Whether the embedder can pull a window out into a desktop window of its own. */
  getCanPopOut(): boolean {
    return this.canPopOut;
  }

  /** The embedder said what it can do (``minds:embedder-capabilities``). */
  setCanPopOut(canPopOut: boolean): void {
    if (this.canPopOut === canPopOut) return;
    this.canPopOut = canPopOut;
    this.notifyListeners();
  }

  /** Whether a window move in progress has pulled ``windowId`` out of the chrome's window (it is hidden meanwhile). */
  isTearingOut(windowId: string): boolean {
    return this.moveGestureOf(windowId)?.isTearingOut === true;
  }

  private moveGestureOf(windowId: string): MoveGesture | null {
    const gesture = this.gesture;
    return gesture !== null && gesture.kind === "move" && gesture.windowId === windowId ? gesture : null;
  }

  /** The chrome reported a step of the drag it watches. "out": the window is detached where it stood, its
   *  frame untouched, and saved at once, so the chrome's own desktop window (which stands in for it under the
   *  cursor from here) reads a placement that already says so, in this shell's stacking order; the ghost
   *  stands where the drag began. "in": the chrome dropped that window, and this one is brought back and shown
   *  again where the drag has it, saved at once too. "released": the button came up while out, and the gesture
   *  is over; the placement is already what it is to be, so nothing is written. This shell is the one writer
   *  during a drag; the chrome's window only reads. A word on a watched drag this shell already ended on its own
   *  release still stands (``takeLateTearOut``). */
  setTearOut(windowId: string, phase: TearOutPhase): void {
    const gesture = this.moveGestureOf(windowId);
    if (gesture === null) {
      this.takeLateTearOut(windowId, phase);
      return;
    }
    if (!gesture.isWatched) return;
    if (phase === "released") {
      if (!gesture.isTearingOut) return;
      this.gesture = null;
      this.notifyListeners();
      return;
    }
    const isTearingOut = phase === "out";
    if (gesture.isTearingOut === isTearingOut) return;
    this.gesture = { ...gesture, isTearingOut, zone: isTearingOut ? null : gesture.zone };
    if (isTearingOut) this.detachDraggedWindow(windowId);
    else this.bringBackDraggedWindow(windowId);
    this.notifyListeners();
  }

  /** Out: the window is detached where it stood, its frame untouched, so the ghost stands where the drag began
   *  and a return lands there. Saved at once, since the chrome's window reads the placement as soon as it loads. */
  private detachDraggedWindow(windowId: string): void {
    this.dispatch({ type: "window_detached", windowId });
    void this.flushPendingSave();
  }

  /** The chrome's word on a watched drag this shell already ended on its own release, which reached it first. The
   *  chrome's word stands: "released" after a release inside means the chrome kept the window it pulled out, so
   *  this one goes out too, from the placement the drag began at, as if "out" had come in time; "in" after a
   *  release outside means the chrome dropped its window, so this one comes back. A late "out" alone changes
   *  nothing: the chrome follows it with "released" when it keeps its window. */
  private takeLateTearOut(windowId: string, phase: TearOutPhase): void {
    const released = this.releasedWatchedDrag;
    if (released === null || released.windowId !== windowId) return;
    if (phase === "released" && !released.isDetached) {
      const start = released.placementAtStart;
      this.dispatch({ type: "window_frame_set", windowId, frame: start.frame });
      if (start.state !== "NORMAL") this.dispatch({ type: "window_state_set", windowId, state: start.state });
      this.detachDraggedWindow(windowId);
    } else if (phase === "in" && released.isDetached) {
      this.bringBackDraggedWindow(windowId);
    } else {
      return;
    }
    this.releasedWatchedDrag = null;
    this.notifyListeners();
  }

  /** Back inside, or cancelled while out: the window is on the desktop again, raised (its state kept, so a
   *  snapped window that un-snapped mid-drag is still where the drag has it), saved at once so the file never
   *  says a window is out while the desktop shows it. */
  private bringBackDraggedWindow(windowId: string): void {
    if (!placementOf(this.state.layout, windowId).is_detached) return;
    this.dispatch({ type: "window_raised", windowId });
    void this.flushPendingSave();
  }

  getState(): DesktopState {
    return this.state;
  }

  getMetrics(): ThemeMetrics {
    return this.metrics;
  }

  getBackdropSize(): PixelSize {
    return this.backdrop;
  }

  getGesture(): ActiveGesture | null {
    return this.gesture;
  }

  isLauncherOpen(): boolean {
    return this.isLauncherOpenNow;
  }

  /** How many times the shell has said what the desktops are; changes only with a ``desktops_updated``. */
  getDesktopsRevision(): number {
    return this.desktopsRevision;
  }

  /** How many times the shell has handed over a layout, which carries this client's paths for independent windows. */
  getLayoutLoadsRevision(): number {
    return this.layoutLoadsRevision;
  }

  gridDimensions(): GridDimensions {
    return gridDimensions(this.backdrop, this.metrics);
  }

  /** Whether this shell can stop and start the app: supervised, not critical, not inside a critical program, and
   *  not from a preview shell, whose stop and start would reach the live workspace's supervisord. */
  canStopApp(app: AppRecord): boolean {
    return !isPreviewShell() && isAppStoppable(this.state, app);
  }

  /** The rectangle a placement renders at, in backdrop pixels (the fit applied). */
  renderedRect(placement: Placement): PixelRect {
    return fitFrameToBackdrop(frameForState(placement.frame, placement.state), this.backdrop, this.metrics);
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => void this.listeners.delete(listener);
  }

  setPageDriver(driver: PageDriver | null): void {
    this.pageDriver = driver;
  }

  dispatch(event: DesktopEvent): void {
    // A solo shell leaves the desktop's arrangement to the client's main window; only its own window's detach
    // and reattach are its to write.
    if (this.soloWindowId !== null && PLACEMENT_EDIT_EVENTS.has(event.type)) {
      const isOwnWindow = "windowId" in event && event.windowId === this.soloWindowId;
      const isOwnVerb = event.type === "window_detached" || event.type === "window_reattached";
      if (!isOwnWindow || !isOwnVerb) return;
    }
    // A phone shows windows without placing them, so a laptop client never sees anything move.
    if (this.isPhoneLayout() && PLACEMENT_EDIT_EVENTS.has(event.type)) return;
    const next = reduceDesktopState(this.state, event);
    if (next === this.state) return;
    // Only an event that changed the layout re-arms the debounce: a broadcast landing while a save
    // waits (every page's location report is one) must not push the save back.
    const isLayoutChanged = next.layoutVersion !== this.state.layoutVersion;
    this.state = next;
    if (isLayoutChanged && isLayoutDirty(next)) this.scheduleSave();
    this.notifyListeners();
    this.followShownWindow();
  }

  /** Whether this shell draws the phone layout: the phone mode, and not a solo shell (which shows its one window
   *  whatever the viewport). */
  isPhoneLayout(): boolean {
    return this.state.modes.isPhone && this.soloWindowId === null;
  }

  /** Keep what the phone shows honest after any change: a shown window that no desktop holds any longer sends the
   *  phone home, a pinned window that is not the active desktop's gives way to its app's pinned window there (the
   *  phone lists only those), and a window an agent op showed before the desktops that hold it arrived is shown once
   *  they do. */
  private followShownWindow(): void {
    if (!this.isPhoneLayout() || !this.state.isDesktopsLoaded) return;
    const pending = this.pendingPhoneShowId;
    if (pending !== null && findWindow(this.state, pending) !== null) {
      this.showOnPhone({ kind: "window", windowId: pending });
      return;
    }
    if (isShownWindowGone(this.state)) {
      // A sheet open when the window went (its own X, Close all, another client's close) stays open over home.
      const sheet = this.state.phone.sheet;
      this.showOnPhone({ kind: "home" });
      if (sheet !== null) this.openPhoneSheet(sheet);
      return;
    }
    const shown = shownWindowOf(this.state);
    if (shown?.is_pinned !== true) return;
    const pinned = pinnedWindowOf(this.state, shown.app);
    if (pinned !== null && pinned.id !== shown.id) this.showOnPhone({ kind: "window", windowId: pinned.id });
  }

  /** Put ``shown`` on the phone's screen and record it with the shell, which keeps it as this client's history: a
   *  reload lands there, and an agent reading the client sees it. Closes any open sheet. */
  showOnPhone(shown: PhoneShown): void {
    this.pendingPhoneShowId = null;
    this.dispatch({ type: "phone_shown", shown });
    this.recordShownWithShell(shown);
  }

  private recordShownWithShell(shown: PhoneShown): void {
    const windowId = shown.kind === "window" ? shown.windowId : null;
    void this.deps.api.recordShown(this.deps.clientId, windowId).catch((error: unknown) => {
      console.warn("[si] could not record what the phone shows", error);
    });
  }

  /** Take the shell's record of what this client showed. A phone showing something the record does not end on (a
   *  show whose recording failed, or one this read overtook) keeps it as the newest entry and records it again, so
   *  the shell, the windows sheet's order and the screen agree. */
  private takeShownHistory(history: readonly string[]): void {
    const shown = this.state.phone.shown;
    if (!this.isPhoneLayout() || shown === null || history[history.length - 1] === shownHistoryEntry(shown)) {
      this.dispatch({ type: "phone_history_loaded", history });
      return;
    }
    this.dispatch({ type: "phone_history_loaded", history: withShownRecorded(history, shownHistoryEntry(shown)) });
    this.recordShownWithShell(shown);
  }

  goHome(): void {
    this.showOnPhone({ kind: "home" });
  }

  /** Show a window an agent op named, now if this client knows it, else once the desktops that hold it arrive. */
  private showOnPhoneWhenKnown(windowId: string): void {
    if (findWindow(this.state, windowId) !== null) {
      this.showOnPhone({ kind: "window", windowId });
      return;
    }
    this.pendingPhoneShowId = windowId;
  }

  /** Where the phone lands: what this client recorded last, else the pinned chat window, else home. Taken without
   *  recording, since the history already says it. */
  private landPhone(): void {
    if (!this.isPhoneLayout() || this.state.phone.shown !== null || !this.state.isDesktopsLoaded) return;
    this.dispatch({ type: "phone_shown", shown: phoneLanding(this.state) });
  }

  openPhoneSheet(sheet: PhoneSheet | null): void {
    this.dispatch({ type: "phone_sheet_set", sheet });
  }

  /** A home tile's tap: the app's window on this client's desktop nearest the top of its stack, else its newest
   *  window anywhere, else the launch of the app's desktop shortcut (``appShortcutOf``), or its first launch path
   *  when it has none. */
  async runHomeTile(appName: string): Promise<void> {
    const target = focusTargetOf(this.state, appName);
    if (target !== null) {
      this.showOnPhone({ kind: "window", windowId: target.id });
      return;
    }
    const app = appByName(this.state, appName);
    const launch = (app === undefined ? null : appShortcutOf(app))?.launch ?? app?.launch_paths[0]?.id;
    if (app === undefined || launch === undefined) {
      this.toast(`Cannot open: ${appName} has nothing to open`);
      return;
    }
    await this.runLaunch(app.name, launch, "new");
  }

  /** Close every window but the pinned one, one after another. */
  async closeAllWindows(): Promise<void> {
    const windows = this.state.desktops.flatMap((desktop) => desktop.windows.filter((window) => !window.is_pinned));
    for (const window of windows) await this.closeWindow(window.id);
  }

  /** The page came back into sight or went out of it. Back in sight, the shell's word is read again, since a
   *  phone's page sleeps while hidden and the pushes of the time away may never have arrived. */
  onVisibilityChange(isVisible: boolean): void {
    if (!isVisible) {
      this.isHiddenSinceConnect = true;
      return;
    }
    void this.refreshInventory();
  }

  private async refreshInventory(): Promise<void> {
    let inventory: Inventory;
    try {
      inventory = await this.deps.api.fetchInventory();
    } catch (error) {
      console.warn("[si] could not read the inventory again", error);
      return;
    }
    this.takeApps(inventory.apps);
    // As a push: a desktop deleted while the page was away moves this client to the fallback, told to the shell.
    this.takeDesktops(inventory.desktops);
    this.dispatch({ type: "workspace_name_updated", workspaceName: inventory.workspace_name });
    const own = inventory.clients.find((client) => client.id === this.deps.clientId);
    if (own !== undefined) this.takeShownHistory(own.shown_history);
  }

  private notifyListeners(): void {
    for (const listener of this.listeners) listener();
    this.deps.redraw();
    this.reportDetachedWindows();
  }

  /** Tell the embedder which windows of the active desktop are pulled out, when that changed. Only once the
   *  layout is known and, in a solo shell, its first load has settled and no reattach save is on its way: a
   *  report without the solo window (before the layout loads, between a first load that does not yet say it
   *  is out and the desktop's word, or before its own reattach has landed) would read, in that window's own
   *  desktop window, as its window having been brought back, and the chrome closes that window on it. */
  private reportDetachedWindows(): void {
    if (!this.state.isLayoutLoaded || this.isSoloFirstLayoutPending || this.isReportHeldForSave) return;
    const report = detachedWindowsOf(this.state);
    const serialized = JSON.stringify(report);
    if (serialized === this.lastReportedDetached) return;
    this.lastReportedDetached = serialized;
    this.popOut.reportDetachedWindows(report);
  }

  setBackdropSize(size: PixelSize): void {
    if (size.width === this.backdrop.width && size.height === this.backdrop.height) return;
    this.backdrop = size;
    this.notifyListeners();
  }

  setThemeMetrics(metrics: ThemeMetrics, modes: RenderModes): void {
    this.metrics = metrics;
    // A gesture of the desktop does not carry over into the phone layout, which has none to finish it.
    if (modes.isPhone) this.cancelGesture();
    // The modes event always yields a new state, so the dispatch notifies and redraws.
    this.dispatch({ type: "render_modes_changed", modes });
    this.landPhone();
  }

  /** Resolves once the app list has landed, with the bootstrap's inventory read or the socket's first
   *  ``apps_updated``, whichever comes first. A ``start`` that failed to read the inventory resolves without
   *  it, so a caller that needs the apps (which of them take the Imbue Studio chrome's messages) waits on this too. */
  whenAppsLoaded(): Promise<void> {
    return this.appsLoaded;
  }

  /** Connect, post this client's arrival, read the inventory (the apps, the desktops, and this client's record),
   *  land on the deep link's desktop else the one the shell answered, fetch the layout, and honour the deep
   *  link's open or launch. The apps come with the inventory rather than waiting on the socket, so a shortcut
   *  is never drawn for an app the page does not know yet. */
  async start(deepLink: DeepLink): Promise<void> {
    this.deps.socket.connect({
      onConnected: () => this.takeConnected(),
      onAppsUpdated: (apps) => this.takeApps(apps),
      onDesktopsUpdated: (desktops) => this.takeDesktops(desktops),
      onPlacementsUpdated: (event) => this.takePlacementsUpdated(event),
      onActiveDesktopChanged: (event) => this.takeActiveDesktopChanged(event),
      onClientEntriesChanged: (event) => this.takeClientEntriesChanged(event),
      onAvatarStatus: (status) => this.dispatch({ type: "avatar_status_updated", status }),
      onAvatarSelectionChanged: (design) => {
        this.avatarSelectionPushes += 1;
        this.dispatch({ type: "avatar_selection_updated", design, defaultDesign: null, pushedAt: performance.now() });
      },
      onUpdateNoticeChanged: (wire) =>
        this.dispatch({ type: "update_notice_changed", notice: wire === null ? null : noticeFromWire(wire) }),
      onLayoutOp: (event) => this.handleLayoutOp(event),
      onPresenceUpdated: (users) => this.takePresence(users),
    });
    void this.loadAvatarSelection();
    const entryPushesBefore = this.entryPushes;
    // The arrival comes first: it may seed a desktop for this user, which the inventory read then includes.
    let arrival: ClientArrival | null;
    try {
      arrival = await this.deps.api.arriveClient(this.deps.clientId);
    } catch (error) {
      console.warn("[si] the shell could not settle where this client lands", error);
      arrival = null;
    }
    let inventory: Inventory;
    try {
      inventory = await this.deps.api.fetchInventory();
    } catch (error) {
      console.warn("[si] could not read the inventory", error);
      this.toast(`Could not read the desktops and apps: ${(error as Error).message}`);
      return;
    }
    // The apps before the desktops, so the first draw of a desktop's shortcuts already knows every app.
    this.takeApps(inventory.apps);
    this.desktopsRevision += 1;
    this.dispatch({ type: "desktops_updated", desktops: inventory.desktops });
    this.dispatch({ type: "workspace_name_updated", workspaceName: inventory.workspace_name });
    this.replacedDesktop = replacedDesktopOf(arrival);
    const own = inventory.clients.find((client) => client.id === this.deps.clientId);
    this.takeFetchedEntries(own, entryPushesBefore);
    if (own !== undefined) {
      this.takeShownHistory(own.shown_history);
      this.hearDesktopRevision(own.desktop_revision);
    }
    // The shell's answer says where this client lands; without one (the arrival failed), the recorded desktop.
    // A solo shell lands on the desktop that holds its window, wherever the client is.
    const landing = arrival?.desktop_id ?? own?.active_desktop ?? null;
    const soloDesktopId =
      this.soloWindowId === null
        ? null
        : (inventory.desktops.find((desktop) => desktop.windows.some((window) => window.id === this.soloWindowId))
            ?.id ?? null);
    // A phone never picks a desktop: its active one is only ever the one the shell assigns on arrival.
    const deepLinkDesktopId = this.isPhoneLayout() ? null : deepLink.desktopId;
    const chosen = chooseInitialDesktopId(inventory.desktops, soloDesktopId ?? deepLinkDesktopId, landing);
    if (chosen === null) return;
    await this.switchDesktop(chosen, "landing");
    this.landPhone();
    if (deepLink.open === null && deepLink.launch === null) return;
    await this.appsLoaded;
    await this.applyDeepLink(deepLink);
  }

  /** The socket (re)opened: the shell hears which desktop this client is on. The first connect leaves
   *  the rest to ``start``; a reconnect resynchronises, since the messages of the time apart are gone
   *  with the socket (the shell resends the apps and desktops itself). */
  private takeConnected(): void {
    // A solo shell's socket is still its client's: registered as a pop-out's, the ops aimed at the client reach it.
    if (this.soloWindowId !== null) this.deps.socket.reportPopOut();
    const wasHidden = this.isHiddenSinceConnect;
    this.isHiddenSinceConnect = false;
    if (this.hasSocketConnected) {
      void this.resyncAfterReconnect();
      const shown = shownWindowOf(this.state);
      if (wasHidden && this.isPhoneLayout() && shown !== null) this.pageDriver?.reload(shown.id);
      return;
    }
    this.hasSocketConnected = true;
    this.reportMove("");
  }

  /** The client record is the shell's word after a reconnect: another window of this client may have
   *  switched desktops meanwhile (the ``active_desktop_changed`` is gone), and reporting this window's
   *  own desktop would move the whole client back to it. So the recorded desktop is adopted as a push
   *  when it differs and is newer than anything this window heard (a record no newer says nothing the
   *  window has not taken, and its own desktop may be a switch whose report went down with the socket,
   *  which the report below then makes), and the layout is read again either way, for the
   *  ``placements_updated`` missed.
   *  The record's entries and the workspace's selection are taken again too, for the
   *  ``client_entries_changed`` and ``avatar_selection_changed`` missed (the server resends the rest). */
  private async resyncAfterReconnect(): Promise<void> {
    let recorded: string | null = null;
    let isRecordedNewer = false;
    const entryPushesBefore = this.entryPushes;
    try {
      const clients = await this.deps.api.fetchClients();
      const own = clients.find((client) => client.id === this.deps.clientId);
      this.takeFetchedEntries(own, entryPushesBefore);
      if (own !== undefined) {
        this.takeShownHistory(own.shown_history);
        isRecordedNewer = this.hearDesktopRevision(own.desktop_revision);
      }
      recorded = own?.active_desktop ?? null;
    } catch (error) {
      console.warn("[si] could not read the client records after reconnecting", error);
    }
    void this.loadAvatarSelection();
    const isRecordedKnown = recorded !== null && this.state.desktops.some((desktop) => desktop.id === recorded);
    // A solo shell stays on its window's desktop: the recorded one is the main window's, and a report from
    // any other desktop would omit the solo window, which the chrome reads as its return.
    const isRecordedAdopted =
      this.soloWindowId === null && isRecordedKnown && isRecordedNewer && recorded !== this.state.activeDesktopId;
    if (recorded !== null && isRecordedAdopted) {
      await this.switchDesktop(recorded, "follow");
      return;
    }
    this.reportMove("");
    await this.refetchLayout();
  }

  private takeApps(apps: readonly AppRecord[]): void {
    this.dispatch({ type: "apps_updated", apps });
    this.markAppsLoaded();
  }

  private takePresence(users: PresentUser[]): void {
    applyPresence(users);
    this.deps.redraw();
  }

  private async applyDeepLink(link: DeepLink): Promise<void> {
    if (link.open !== null) {
      if (appByName(this.state, link.open.app) === undefined)
        console.warn(`[si] deep link ignored: no app ${link.open.app}`);
      else await this.openWindowAt(link.open.app, link.open.path, "focus");
    }
    if (link.launch !== null) {
      const app = appByName(this.state, link.launch.app);
      const launchPath = app === undefined ? null : launchPathOf(app, link.launch.launch);
      if (app === undefined || launchPath === null) {
        console.warn(`[si] deep link ignored: no launch path ${link.launch.app}:${link.launch.launch}`);
      } else {
        await this.launchAt(app.name, launchPath.id, {}, { kind: "new" });
      }
    }
  }

  /** Tell the shell this window moved the client onto the desktop it is on, leaving ``previousDesktop`` ("" when it
   *  left none). */
  private reportMove(previousDesktop: string): void {
    const active = this.reportedDesktopId();
    if (active === null) return;
    this.deps.socket.reportClientState({
      activeDesktop: active,
      previousDesktop,
      reportId: this.reportIds.mint(),
      isFollowing: false,
    });
  }

  /** Tell the shell this window followed the client's stored desktop, which registers it there and moves nothing. */
  private reportFollowedDesktop(): void {
    const active = this.reportedDesktopId();
    if (active === null) return;
    this.deps.socket.reportClientState({
      activeDesktop: active,
      previousDesktop: "",
      reportId: null,
      isFollowing: true,
    });
  }

  /** The desktop this window reports as its own, null when it reports none. */
  private reportedDesktopId(): string | null {
    // A solo shell sits on its window's desktop without moving the client there: the client's active desktop
    // is its main window's.
    if (this.soloWindowId !== null) return null;
    return this.state.activeDesktopId;
  }

  /** Take note of a revision of the client's stored desktop; answers whether it is newer than any heard before. */
  private hearDesktopRevision(revision: number): boolean {
    if (revision <= this.desktopRevisionHeard) return false;
    this.desktopRevisionHeard = revision;
    return true;
  }

  private takeDesktops(desktops: readonly Desktop[]): void {
    const previous = this.state.activeDesktopId;
    this.desktopsRevision += 1;
    this.dispatch({ type: "desktops_updated", desktops });
    if (this.state.activeDesktopId !== previous) {
      // The active desktop was deleted and the reducer landed on the fallback: the window reports that landing as
      // a move, as the page's own landing does, so the client's record names the desktop the user now sees.
      this.cancelGesture();
      this.reportMove("");
      void this.refetchLayout();
    }
  }

  private takePlacementsUpdated(event: PlacementsUpdatedEvent): void {
    if (event.clientId !== this.deps.clientId || event.desktopId !== this.state.activeDesktopId) return;
    if (this.saveIds.isOwn(event.saveId)) return;
    void this.refetchLayout();
  }

  /** Follow the client's stored desktop when it moved, unless the news is stale: a revision no newer than one this
   *  window heard was overtaken by a later move on its way here, and the echo of a switch this window reported and
   *  has replaced since with another is followed by the later one's own echo. */
  private takeActiveDesktopChanged(event: ActiveDesktopChangedEvent): void {
    if (event.clientId !== this.deps.clientId || !this.hearDesktopRevision(event.revision)) return;
    if (event.reportId !== null && this.reportIds.isSuperseded(event.reportId)) return;
    // News naming the desktop shown still goes to switchDesktop: an earlier follow waiting on a save has yet to move
    // the window, and switchDesktop compares against the desktop only once that save is done.
    // A solo shell stays on its window's desktop whatever the client's main window switches to.
    if (this.soloWindowId !== null) return;
    void this.switchDesktop(event.desktopId, "follow");
  }

  private takeClientEntriesChanged(event: ClientEntriesChangedEvent): void {
    if (event.clientId !== this.deps.clientId) return;
    this.entryPushes += 1;
    this.dispatch({ type: "entries_updated", entries: event.entries });
  }

  /** This client's entries as its fetched record carries them, unless a push landed while the records were
   *  read: the push is newer than the answer. */
  private takeFetchedEntries(own: ClientRecord | undefined, entryPushesBefore: number): void {
    if (own === undefined || this.entryPushes !== entryPushesBefore) return;
    this.dispatch({ type: "entries_updated", entries: own.entries });
  }

  /** The workspace's design and the fallback, from the catalog; a read that fails leaves the initial ones, and
   *  a selection pushed while the catalog was read stands over the catalog's older answer. */
  private async loadAvatarSelection(): Promise<void> {
    const pushesBefore = this.avatarSelectionPushes;
    try {
      const catalog = await this.deps.api.fetchAvatars();
      const design = this.avatarSelectionPushes === pushesBefore ? catalog.selected : this.state.avatar.design;
      this.dispatch({ type: "avatar_selection_updated", design, defaultDesign: catalog.default, pushedAt: null });
    } catch (error) {
      console.warn("[si] could not read the avatar designs", error);
    }
  }

  /** The designs on offer, read anew on every call so one an agent registered meanwhile is listed. */
  async fetchAvatarDesigns(): Promise<readonly AvatarDesign[]> {
    return (await this.deps.api.fetchAvatars()).designs;
  }

  /** Hand ``text`` to the pinned window that takes a draft (plan section 4.7): its app's draft launch path runs
   *  into this client's view of that window with the text as the draft param, and the window is restored and
   *  raised; the app drafts the text into a composer and the page reports where it landed. False when no pinned
   *  app on this desktop takes one, or the launch was refused. */
  async draftIntoPinnedWindow(text: string): Promise<boolean> {
    const target = draftTargetOf(this.state);
    if (target === null) return false;
    const launched = await this.launchAt(
      target.window.app,
      target.launchPath.id,
      freeTextParams(target.launchPath, text),
      { kind: "window", windowId: target.window.id },
    );
    this.restoreWindow(target.window.id);
    return launched !== null;
  }

  /** A message from the Imbue Studio chrome: when an app registered for its type, the shell is asked, once, to post it
   *  there with this client's id (contracts.md section 5.6); the app decides what it means. False when no app
   *  registered for the type, the shell could not pass it on, this is a preview shell (whose backend refuses
   *  the relay: the apps it names are the live ones), or this is a solo shell (whose client is the main window's,
   *  so what an app did with the message would land there). */
  async relayEmbedderMessage(message: EmbedderMessage): Promise<boolean> {
    if (isPreviewShell() || this.soloWindowId !== null || !isEmbedderMessageHandled(this.state, message.type)) {
      return false;
    }
    const { type, ...payload } = message;
    try {
      await this.deps.api.relayEmbedderMessage(type, this.deps.clientId, payload);
    } catch (error) {
      console.warn(`[si] could not relay ${type} from the embedder`, error);
      return false;
    }
    return true;
  }

  /** Point this client's view of a window at ``path``, the way an agent's ``navigate`` does: the location is
   *  written through the shell and the answer applied as a load rather than as the page's own report, so the
   *  following step moves the page there (a page's own report is the one thing the following never bounces back,
   *  and applying it that way would leave the page where it was). False when the shell refused. */
  async navigateOwnWindow(windowId: string, path: string): Promise<boolean> {
    const found = findWindow(this.state, windowId);
    if (found === null) return false;
    const title = effectiveWindowTitle(this.state, found.window, appByName(this.state, found.window.app));
    let reported: WindowRecord;
    try {
      reported = await this.deps.api.reportWindowLocation(found.desktop.id, windowId, this.deps.clientId, path, title);
    } catch (error) {
      this.toast(`Could not move the window: ${(error as Error).message}`);
      return false;
    }
    this.applyOwnNavigation(found, reported);
    return true;
  }

  /** Take the window the shell answered a navigation of this client's own with, as a load the pages follow. */
  private applyOwnNavigation(found: { desktop: Desktop; window: WindowRecord }, reported: WindowRecord): void {
    // Marked only once the answer is applied, for the one follow that application triggers: set any earlier, a
    // refusal or a broadcast landing meanwhile would leave the mark to lift the guard for some other follow.
    if (found.window.scope === "independent") {
      if (found.desktop.id !== this.state.activeDesktopId) return;
      this.ownNavigation = { windowId: found.window.id, path: reported.path };
      this.layoutLoadsRevision += 1;
      this.dispatch({
        type: "window_paths_loaded",
        desktopId: found.desktop.id,
        windowPaths: {
          ...this.state.layout.window_paths,
          [found.window.id]: { path: reported.path, title: reported.title },
        },
      });
      return;
    }
    this.ownNavigation = { windowId: found.window.id, path: reported.path };
    this.desktopsRevision += 1;
    this.dispatch({ type: "window_location_reported", desktopId: found.desktop.id, window: reported });
  }

  /** The navigation this client asked for itself since the pages last followed, handed over once. */
  takeOwnNavigation(): OwnNavigation | null {
    const own = this.ownNavigation;
    this.ownNavigation = null;
    return own;
  }

  /** Choose the workspace's avatar design; every window (this one included) follows the shell's broadcast. */
  async selectAvatar(design: string): Promise<void> {
    try {
      await this.deps.api.selectAvatar(design);
    } catch (error) {
      this.toast(`Could not change the avatar: ${(error as Error).message}`);
    }
  }

  /** Write how this client shows a pinned entry (its mode, style, and floating position). Applied at once, so a
   *  released drag lands where it was dropped rather than at the old spot until the shell answers. A push that
   *  lands while the shell answers is its newer word (the shell announces every write, this one included, before
   *  answering it) and stands: without one, the record the shell answers is taken, and a refusal puts that
   *  entry's old presentation back (another entry written meanwhile is not undone with it). */
  async setEntryPresentation(app: string, presentation: EntryPresentation): Promise<void> {
    const previous = this.state.entries[app];
    const entryPushesBefore = this.entryPushes;
    this.dispatch({ type: "entries_updated", entries: { ...this.state.entries, [app]: presentation } });
    try {
      const record = await this.deps.api.setEntryPresentation(this.deps.clientId, app, presentation);
      if (this.entryPushes !== entryPushesBefore) return;
      this.dispatch({ type: "entries_updated", entries: record.entries });
    } catch (error) {
      this.toast(`Could not change the entry: ${(error as Error).message}`);
      if (this.entryPushes !== entryPushesBefore) return;
      const others = Object.fromEntries(Object.entries(this.state.entries).filter(([name]) => name !== app));
      this.dispatch({
        type: "entries_updated",
        entries: previous === undefined ? others : { ...others, [app]: previous },
      });
    }
  }

  /** The presentation a write of one field starts from: the entry's current look. */
  private presentationOf(app: string): EntryPresentation | null {
    const window = pinnedWindowOf(this.state, app);
    if (window === null) return null;
    const look = entryLook(this.state, window, appByName(this.state, app));
    return look === null ? null : { mode: look.mode, style: look.style, position: look.position };
  }

  setEntryMode(app: string, mode: EntryMode): Promise<void> {
    const current = this.presentationOf(app);
    return current === null ? Promise.resolve() : this.setEntryPresentation(app, { ...current, mode });
  }

  setEntryStyle(app: string, style: PinStyle): Promise<void> {
    const current = this.presentationOf(app);
    return current === null ? Promise.resolve() : this.setEntryPresentation(app, { ...current, style });
  }

  /** Where a floating entry's box is right now, in backdrop pixels: the drag's rectangle while one moves it,
   *  else its stored position (the default corner when it has none), clamped into the backdrop. */
  renderedFloatingEntryRect(app: string, position: FloatingPosition | null): PixelRect {
    const gesture = this.gesture;
    if (gesture !== null && gesture.kind === "floating-entry" && gesture.app === app) return gesture.currentRect;
    return floatingEntryRect(
      position ?? defaultFloatingPosition(this.backdrop, this.metrics),
      this.backdrop,
      this.metrics,
    );
  }

  handleLayoutOp(event: LayoutOpEvent): void {
    switch (event.op) {
      case "refresh": {
        const windowId = event.args.window;
        const appName = event.args.app;
        if (typeof windowId === "string" && windowId !== "") this.pageDriver?.reload(windowId);
        else if (typeof appName === "string" && appName !== "") this.pageDriver?.reloadApp(appName);
        return;
      }
      case "reload_system_interface":
        this.deps.reloadInterface();
        return;
      case "show":
      case "open":
      case "focus": {
        // An op that put a window on this client's screen. A phone shows it; a desktop has it placed already, and
        // acts only on a pulled-out window the shell left out, whose desktop window it raises as the taskbar's
        // "Show" does, from the main window's page (a solo page shares its client).
        const windowId = event.args.window;
        if (this.soloWindowId !== null || typeof windowId !== "string" || windowId === "") return;
        if (this.isPhoneLayout()) this.showOnPhoneWhenKnown(windowId);
        else if (event.op === "show" && event.args.is_detached === true) this.showDetachedWindow(windowId);
        return;
      }
    }
  }

  /** Switch this window onto ``desktopId``: flush the outgoing layout's pending save, tell the shell,
   *  and fetch the incoming layout. The user's switch moves the client and names the desktop it left; the
   *  bootstrap's landing moves it naming none; a follow of the stored desktop moves nothing. */
  async switchDesktop(desktopId: string, cause: SwitchCause = "user"): Promise<void> {
    // The desktop left is read after the save: an earlier switch waiting on the same save moves the window first.
    await this.flushPendingSave();
    const previous = this.state.activeDesktopId;
    if (previous === desktopId) return;
    this.cancelGesture();
    this.dispatch({ type: "desktop_activated", desktopId });
    switch (cause) {
      case "user":
        this.reportMove(previous ?? "");
        break;
      case "landing":
        this.reportMove("");
        break;
      case "follow":
        this.reportFollowedDesktop();
        break;
    }
    await this.refetchLayout();
  }

  async createDesktop(name: string, color: string, glyph: number): Promise<void> {
    try {
      const created = await this.deps.api.createDesktop(name, color, glyph);
      // The broadcast may have landed first: upsert rather than append.
      this.takeDesktop(created);
      await this.switchDesktop(created.id);
    } catch (error) {
      this.toast(`Could not create the desktop: ${(error as Error).message}`);
    }
  }

  async updateDesktopSettings(desktopId: string, name: string, color: string, glyph: number): Promise<void> {
    this.takeDesktop(await this.deps.api.updateDesktopSettings(desktopId, name, color, glyph));
  }

  /** This user's deleted desktop and the one seeded in its place, while the notice about them is still owed. */
  getReplacedDesktop(): ReplacedDesktop | null {
    return this.replacedDesktop;
  }

  dismissReplacedDesktopNotice(): void {
    this.replacedDesktop = null;
    this.deps.redraw();
  }

  async setDesktopWallpaper(desktopId: string, wallpaper: Wallpaper | null): Promise<void> {
    this.takeDesktop(await this.deps.api.setDesktopWallpaper(desktopId, wallpaper));
  }

  /** Delete a desktop; the shell moves this client to the fallback and says so over the socket. */
  async deleteDesktop(desktopId: string): Promise<void> {
    await this.deps.api.deleteDesktop(desktopId);
  }

  private takeDesktop(desktop: Desktop): void {
    const desktops = this.state.desktops.some((candidate) => candidate.id === desktop.id)
      ? this.state.desktops.map((candidate) => (candidate.id === desktop.id ? desktop : candidate))
      : [...this.state.desktops, desktop];
    this.dispatch({ type: "desktops_updated", desktops });
  }

  /** Add a launch path to the active desktop at the first free cell in reading order over the current grid, drawn
   *  at once and taken off again if the shell refuses; nothing when it is already there (the shell would move the
   *  shortcut and reset its mode). A refusal takes off only the shortcut drawn here: one a broadcast put there in
   *  the meantime is the shell's and stays. */
  async addShortcut(app: string, launch: string, mode: ShortcutMode): Promise<void> {
    const desktop = activeDesktop(this.state);
    if (desktop === null) return;
    if (findShortcut(desktop, app, launch) !== undefined) return;
    const cell = cellForAddedShortcut(desktop, this.gridDimensions());
    const shortcut: DesktopShortcut = { target: { kind: "launch", app, launch }, mode, cell };
    this.takeDesktop({ ...desktop, shortcuts: [...desktop.shortcuts, shortcut] });
    try {
      this.takeDesktop(await this.deps.api.setDesktopShortcut(desktop.id, shortcut));
    } catch (error) {
      const now = desktopById(this.state, desktop.id);
      if (now !== null && findShortcut(now, app, launch) === shortcut) {
        this.takeDesktop({ ...now, shortcuts: now.shortcuts.filter((candidate) => candidate !== shortcut) });
      }
      this.toast(`Could not add the shortcut: ${(error as Error).message}`);
    }
  }

  /** Replace a shortcut the desktop holds, as the shortcut menu's mode flip does. */
  async setShortcut(desktopId: string, shortcut: DesktopShortcut): Promise<void> {
    try {
      this.takeDesktop(await this.deps.api.setDesktopShortcut(desktopId, shortcut));
    } catch (error) {
      this.toast(`Could not change the shortcut: ${(error as Error).message}`);
    }
  }

  /** Drop a dragged shortcut into ``cell``, showing the arrangement the drag drew at once and putting the
   *  cells back if the shell refuses: a drop that waited on the round trip would draw the shortcuts in the
   *  cells they came from meanwhile.
   *
   *  Only the dragged shortcut is named: the shell displaces whatever was in the cell by the same rule and
   *  its answer is what stands. It searches the unbounded plane where the drag searched the grid, so on a
   *  desktop with no free cell near the target the shortcut settles where the shell put it rather than
   *  where the drag drew it (contracts.md section 10).
   *
   *  Both writes move the shortcuts of whatever the desktop is by then, rather than restoring a snapshot
   *  taken before the request: a later drop, or a broadcast that landed in between, is someone else's edit
   *  and is not this refusal's to undo. */
  private async dropShortcut(
    app: string,
    launch: string,
    cell: GridCell,
    shown: readonly PlacedShortcut[],
  ): Promise<void> {
    const desktop = activeDesktop(this.state);
    if (desktop === null) return;
    // What the room made moved, against the cells the shortcuts were already drawn in: a drop that moved
    // nothing (back into the cell it came from) is not worth a round trip, and a cell the placement re-fitted
    // is where that shortcut was drawn all along rather than anything this drag decided.
    const restingCellByKey = new Map(this.restingShortcuts().map((entry) => [placedShortcutKey(entry), entry.cell]));
    const changes = shown.flatMap((entry) => {
      const resting = restingCellByKey.get(placedShortcutKey(entry));
      if (resting === undefined || isSameCell(entry.cell, resting)) return [];
      const target = entry.shortcut.target;
      return [{ app: target.app, launch: target.launch, cell: entry.cell, storedCell: entry.shortcut.cell }];
    });
    if (changes.length === 0) return;
    this.withShortcutCells(changes);
    try {
      this.takeDesktop(await this.deps.api.moveDesktopShortcut(desktop.id, app, launch, cell));
    } catch (error) {
      this.withShortcutCells(changes.map((entry) => ({ ...entry, cell: entry.storedCell })));
      this.toast(`Could not move the shortcut: ${(error as Error).message}`);
    }
  }

  /** The active desktop with the named shortcuts' cells rewritten, taken as the desktop of record. */
  private withShortcutCells(cells: readonly ShortcutCellUpdate[]): void {
    const desktop = activeDesktop(this.state);
    if (desktop === null) return;
    this.takeDesktop({
      ...desktop,
      shortcuts: desktop.shortcuts.map((shortcut) => {
        const wanted = cells.find((entry) => isShortcutOf(shortcut, entry.app, entry.launch));
        return wanted === undefined ? shortcut : { ...shortcut, cell: wanted.cell };
      }),
    });
  }

  /** Take a shortcut off the active desktop at once, putting it back if the shell refuses. A refusal puts it back
   *  only while the shell has said nothing of the desktops since: a broadcast in the meantime is the shell's word on
   *  whether it is still there, and stands. */
  async removeShortcut(app: string, launch: string): Promise<void> {
    const desktop = activeDesktop(this.state);
    if (desktop === null) return;
    const removed = findShortcut(desktop, app, launch);
    if (removed === undefined) return;
    this.takeDesktop({ ...desktop, shortcuts: desktop.shortcuts.filter((candidate) => candidate !== removed) });
    const revision = this.desktopsRevision;
    try {
      this.takeDesktop(await this.deps.api.removeDesktopShortcut(desktop.id, app, launch));
    } catch (error) {
      const now = desktopById(this.state, desktop.id);
      if (this.desktopsRevision === revision && now !== null && findShortcut(now, app, launch) === undefined) {
        this.takeDesktop({ ...now, shortcuts: [...now.shortcuts, removed] });
      }
      this.toast(`Could not remove the shortcut: ${(error as Error).message}`);
    }
  }

  /** Run a shortcut in its mode: focus raises the app's most recent window and opens only when there is none.
   *  Before the apps are known (the inventory has not answered) the user is told to wait rather than that
   *  the app is missing. */
  async runLaunch(app: string, launch: string, mode: ShortcutMode): Promise<void> {
    const run = resolveLaunchRun(this.state, app, launch, mode);
    switch (run.kind) {
      case "raise":
        this.restoreWindow(run.windowId);
        return;
      case "open":
        await this.launchAt(run.app, run.launch, {}, { kind: "new" });
        return;
      case "connecting":
        this.toast(STILL_CONNECTING_NOTICE);
        return;
      case "unavailable":
        this.toast(`Cannot open: ${run.reason}`);
        return;
    }
  }

  runShortcut(shortcut: DesktopShortcut): Promise<void> {
    return this.runLaunch(shortcut.target.app, shortcut.target.launch, shortcut.mode);
  }

  /** The app and launch path a launcher row names, or null (told to the user) when the app declares no such path. */
  private launchOf(appName: string, launchId: string): { app: AppRecord; launchPath: LaunchPath } | null {
    const app = appByName(this.state, appName);
    const launchPath = app === undefined ? null : launchPathOf(app, launchId);
    if (app === undefined || launchPath === null) {
      this.toast(`Cannot open: ${appName} has no launch path ${launchId}`);
      return null;
    }
    return { app, launchPath };
  }

  /** Run a launch-path row (launcher plan section 3.3): a GET launch path at its app's pin path raises the pinned
   *  window on the active desktop and opens nothing; every other launches into a new window. */
  async runLaunchRow(appName: string, launchId: string): Promise<void> {
    const found = this.launchOf(appName, launchId);
    if (found === null) return;
    if (launchRowKindOf(found.app, found.launchPath) === "focus") {
      const pinned = pinnedWindowOf(this.state, found.app.name);
      if (pinned !== null) {
        this.restoreWindow(pinned.id);
        return;
      }
    }
    await this.launchAt(found.app.name, found.launchPath.id, {}, { kind: "new" });
  }

  /** Run a free-text row with ``text`` (launcher plan section 3.2, post-launch-paths plan section 4.1),
   *  pinned-first: when the app has a pinned window on the active desktop, the launch runs into this client's view
   *  of it (the page the launch answers is pure, so a linked window is safe too) and the window is restored and
   *  raised; otherwise the launch opens a new window. False when the text cannot go (a GET launch path over the
   *  path bound) or the shell refused, each told to the user. */
  async runFreeText(appName: string, launchId: string, text: string): Promise<boolean> {
    const found = this.launchOf(appName, launchId);
    if (found === null) return false;
    const disabledReason = textRowDisabledReason(found.launchPath, text);
    if (disabledReason !== null) {
      this.toast(disabledReason);
      return false;
    }
    const params = freeTextParams(found.launchPath, text);
    const pinned = pinnedWindowOf(this.state, found.app.name);
    if (pinned !== null) {
      const launched = await this.launchAt(found.app.name, found.launchPath.id, params, {
        kind: "window",
        windowId: pinned.id,
      });
      this.restoreWindow(pinned.id);
      return launched !== null;
    }
    return (await this.launchAt(found.app.name, found.launchPath.id, params, { kind: "new" })) !== null;
  }

  /** A page's ``shell:draft-text`` (element-reference-menu plan section 6): the text is drafted into the pinned
   *  window that takes a draft, else through the first draft row of the machine; with neither the user is told. */
  async draftText(text: string): Promise<boolean> {
    if (draftTargetOf(this.state) !== null) return this.draftIntoPinnedWindow(text);
    const [draftRow] = draftRowsOf(openableApps(this.state));
    if (draftRow === undefined) {
      this.toast(NO_DRAFT_APP_REASON);
      return false;
    }
    return this.runFreeText(draftRow.app.name, draftRow.launchPath.id, text);
  }

  /** A page's ``shell:start-with-text`` (launcher plan section 3.7): the primary text action runs with the text;
   *  with no free-text row on the machine the user is told. */
  async startWithText(text: string): Promise<boolean> {
    const [primary] = freeTextRowsOf(openableApps(this.state));
    if (primary === undefined) {
      this.toast(NO_TEXT_APP_REASON);
      return false;
    }
    return this.runFreeText(primary.app.name, primary.launchPath.id, text);
  }

  /** The desktop an open goes to: the active one, or on a phone the first, where a window opened out of sight
   *  is minimized at the bottom of every other client's stack and nobody's own desktop moves. */
  private openingDesktopId(): string | null {
    if (this.isPhoneLayout()) return this.state.desktops[0]?.id ?? null;
    return this.state.activeDesktopId;
  }

  /** Every open goes through the shell's one route; the answer is applied at once and the layout
   *  refetched for the stamp the shell wrote. A phone opens out of sight and shows the window itself. Answers the
   *  window id, or null when the shell refused. */
  async openWindowAt(app: string, path: string, ifPresent: IfPresent): Promise<string | null> {
    const desktopId = this.openingDesktopId();
    if (desktopId === null) return null;
    const isMinimized = this.isPhoneLayout();
    // The shell writes the new placement over the stored layout, and the refetch takes that: a gesture
    // still waiting in the debounce goes into the file first or it is lost.
    await this.flushPendingSave();
    let outcome: WindowOpenOutcome;
    try {
      outcome = await this.deps.api.openWindow(desktopId, {
        app,
        path,
        clientId: this.deps.clientId,
        ifPresent,
        isMinimized,
      });
    } catch (error) {
      this.toast(`Could not open ${app}: ${(error as Error).message}`);
      return null;
    }
    this.takeOpened(desktopId, outcome.window, outcome.isNew, isMinimized);
    return outcome.window.id;
  }

  /** A window the shell opened or answered for this client, applied at once; the phone shows it. */
  private takeOpened(desktopId: string, window: WindowRecord, isNew: boolean, isMinimized: boolean): void {
    this.dispatch({ type: "window_opened_here", desktopId, window, isNew, isMinimized });
    void this.refetchLayout();
    if (this.isPhoneLayout()) this.showOnPhone({ kind: "window", windowId: window.id });
  }

  /** Run a launch path through the shell's launch route (post-launch-paths plan section 5.3): the shell resolves the
   *  page (built for a GET launch path, asked of the app for a POST one) and opens a window there or points the
   *  named window at it. An opened or focused window is taken as an open is; a navigated one as this client's own
   *  navigation, so the page follows. Answers the window's id, or null when the shell refused (told to the user). */
  async launchAt(
    app: string,
    launchId: string,
    params: Readonly<Record<string, string>>,
    target: LaunchTarget,
  ): Promise<string | null> {
    // A launch into a named window runs on that window's desktop; any other on the desktop an open goes to.
    const named = target.kind === "window" ? findWindow(this.state, target.windowId) : null;
    const desktopId = named?.desktop.id ?? this.openingDesktopId();
    if (desktopId === null) return null;
    const isMinimized = this.isPhoneLayout();
    await this.flushPendingSave();
    let outcome: LaunchOutcome;
    try {
      outcome = await this.deps.api.launch(desktopId, {
        app,
        launch: launchId,
        params,
        clientId: this.deps.clientId,
        target,
        isMinimized,
      });
    } catch (error) {
      this.toast(`Could not open ${app}: ${(error as Error).message}`);
      return null;
    }
    if (target.kind === "window") {
      if (named !== null) this.applyOwnNavigation(named, outcome.window);
      return outcome.window.id;
    }
    this.takeOpened(desktopId, outcome.window, outcome.isNew, isMinimized);
    return outcome.window.id;
  }

  /** ``shell:open {path, ifPresent}`` from a page: a window of the posting window's own app, on its desktop. */
  async openPathFromWindow(windowId: string, path: string, ifPresent: IfPresent): Promise<void> {
    const found = findWindow(this.state, windowId);
    if (found === null) return;
    if (!this.isPhoneLayout() && found.desktop.id !== this.state.activeDesktopId) {
      await this.switchDesktop(found.desktop.id);
    }
    await this.openWindowAt(found.window.app, path, ifPresent);
  }

  async closeWindow(windowId: string): Promise<void> {
    const found = findWindow(this.state, windowId);
    if (found === null) return;
    // The shell drops the window from this client's stored layout and stamps the rewrite, and the refetch
    // its broadcast triggers takes that: a gesture still waiting in the debounce goes into the file first
    // or it is lost.
    await this.flushPendingSave();
    try {
      await this.deps.api.closeWindow(found.desktop.id, windowId);
    } catch (error) {
      this.toast(`Could not close the window: ${(error as Error).message}`);
      return;
    }
    this.dispatch({ type: "window_closed_here", desktopId: found.desktop.id, windowId });
  }

  /** The Imbue Studio close chord: the focused window is told, then closed for everyone; a pinned window, which is never
   *  closed, is minimized instead. */
  async closeFocusedWindow(): Promise<void> {
    // In a solo shell the chord belongs to the chrome, which closes the desktop window instead; the focused
    // window here would be some other window of the desktop.
    if (this.soloWindowId !== null) return;
    // A phone's focused window is the one it shows.
    const focused = this.isPhoneLayout() ? (shownWindowOf(this.state)?.id ?? null) : activeFocusedWindowId(this.state);
    if (focused === null) return;
    if (findWindow(this.state, focused)?.window.is_pinned === true) {
      this.minimizeWindow(focused);
      return;
    }
    this.pageDriver?.requestClose(focused);
    // A page that owns the chord (a browser closing one of its tabs) keeps its window.
    if (this.pageDriver?.ownsCloseChord(focused) === true) return;
    await this.closeWindow(focused);
  }

  /** What every human-facing Close does (the title bar's control, the two menus): close the window for everyone,
   *  or, for a pinned window, which is never closed, minimize it, so the habit of reaching for the control holds. */
  async closeOrMinimizeWindow(windowId: string): Promise<void> {
    if (findWindow(this.state, windowId)?.window.is_pinned === true) {
      this.minimizeWindow(windowId);
      return;
    }
    await this.closeWindow(windowId);
  }

  /** Raise a window to the top of the stack, unless a move or resize of another window is in progress: the
   *  gestured window holds the top until its gesture ends, and a raise that arrives meanwhile (a page reporting
   *  focus as the chrome window's focus comes back mid-drag) is not the user choosing that window. Dropped
   *  rather than deferred, since the gesture's end raises its own window again. */
  raiseWindow(windowId: string): void {
    if (this.isAnotherWindowGestured(windowId)) return;
    this.dispatch({ type: "window_raised", windowId });
  }

  private isAnotherWindowGestured(windowId: string): boolean {
    const gesture = this.gesture;
    if (gesture === null) return false;
    return (gesture.kind === "move" || gesture.kind === "resize") && gesture.windowId !== windowId;
  }

  minimizeWindow(windowId: string): void {
    this.dispatch({ type: "window_minimized", windowId });
  }

  /** Restore a minimized window (the taskbar's verb): raised; on a phone, shown. */
  restoreWindow(windowId: string): void {
    if (this.isPhoneLayout()) {
      this.showOnPhone({ kind: "window", windowId });
      return;
    }
    this.raiseWindow(windowId);
  }

  setWindowState(windowId: string, state: WindowState): void {
    this.dispatch({ type: "window_state_set", windowId, state });
  }

  /** Place a window at a fraction of the backdrop -- the size menu's halves and quarters, which no
   *  window state stands for. Normal, shown and raised, as a drag that ends away from an edge leaves it. */
  setWindowFrame(windowId: string, frame: Frame): void {
    this.dispatch({ type: "window_frame_set", windowId, frame });
  }

  toggleMaximized(windowId: string): void {
    const placement = placementOf(this.state.layout, windowId);
    if (placement.state === "MAXIMIZED") this.dispatch({ type: "window_restored", windowId });
    else this.dispatch({ type: "window_state_set", windowId, state: "MAXIMIZED" });
  }

  /** Pull a window out into a desktop window of the embedder's own without a drag (the window menu's "Open in
   *  its own window"): the chrome opens it beside its window, at the size the window renders here. The
   *  placement is saved first, since the chrome's new window reads it as soon as it loads. */
  async detachWindow(windowId: string): Promise<void> {
    if (!this.canPopOut || !this.isPopOutVerbOwn(windowId) || findWindow(this.state, windowId) === null) return;
    this.dispatch({ type: "window_detached", windowId });
    await this.flushPendingSave();
    this.showDetachedWindow(windowId);
  }

  /** Show the ghost of a pulled-out window again where it stood, after ``minimizeWindow`` hid it (the ghost's
   *  "Hide"); the window itself stays in the chrome's desktop window. */
  showWindowGhost(windowId: string): void {
    this.dispatch({ type: "window_detached", windowId });
  }

  /** Raise (or reopen) the desktop window a pulled-out window is shown in, at the size the window renders here. */
  showDetachedWindow(windowId: string): void {
    if (findWindow(this.state, windowId) === null) return;
    const rect = this.renderedRect(placementOf(this.state.layout, windowId));
    this.popOut.requestPopOut(this.popOutRequestFor(windowId, rect));
  }

  /** What the chrome is asked for when a window is pulled out: the window's title and the size its desktop
   *  window is to have. */
  private popOutRequestFor(windowId: string, size: PixelSize): PopOutRequest {
    const found = findWindow(this.state, windowId);
    const title =
      found === null ? "" : effectiveWindowTitle(this.state, found.window, appByName(this.state, found.window.app));
    return { windowId, title, width: size.width, height: size.height };
  }

  /** Tell the chrome about a title-bar drag it is to watch: the window as it renders at ``rect``, held at
   *  ``pointer``. Sent again when the dragged window changes size (a snapped window un-snapping). */
  private announceWindowDrag(windowId: string, rect: PixelRect, pointer: PixelPoint): void {
    const grab = {
      x: Math.min(Math.max(pointer.x - rect.x, 0), rect.width),
      y: Math.min(Math.max(pointer.y - rect.y, 0), rect.height),
    };
    this.popOut.beginWindowDrag({ ...this.popOutRequestFor(windowId, rect), grabX: grab.x, grabY: grab.y });
  }

  /** Bring a pulled-out window back to the desktop (``minds:reattach-window``, the ghost's "Bring back", the
   *  entry's menu): shown and raised, at ``frame`` when a drop back onto the desktop named one. The window's own
   *  desktop is shown first when the client has moved to another meanwhile (the chrome names a window, not a
   *  desktop). Saved at once, and in a solo shell reported only once saved: the chrome closes that shell's
   *  desktop window on the report, which would abort a save still on its way. */
  async reattachWindow(windowId: string, frame: Frame | null): Promise<void> {
    if (!this.isPopOutVerbOwn(windowId)) return;
    const found = findWindow(this.state, windowId);
    if (found === null) return;
    if (found.desktop.id !== this.state.activeDesktopId) await this.switchDesktop(found.desktop.id);
    this.isReportHeldForSave = this.soloWindowId !== null;
    this.dispatch({ type: "window_reattached", windowId, frame });
    await this.flushPendingSave();
    this.isReportHeldForSave = false;
    this.reportDetachedWindows();
  }

  /** Whether a pull-out verb for ``windowId`` is this shell's to apply: any window's in a desktop shell, and only
   *  its own window's in a solo shell, whose desktop and arrangement belong to the client's main window. Checked
   *  before the verb's side effects (a desktop switch, an ask of the chrome), which ``dispatch`` cannot refuse. */
  private isPopOutVerbOwn(windowId: string): boolean {
    return this.soloWindowId === null || windowId === this.soloWindowId;
  }

  /** A taskbar entry's click: for a pulled-out window, show its ghost again when the ghost is hidden, else its
   *  own desktop window; otherwise restore and raise when minimized, minimize when focused, raise otherwise. */
  toggleTaskbarEntry(windowId: string): void {
    const placement = placementOf(this.state.layout, windowId);
    if (placement.is_detached) {
      if (placement.is_minimized) this.showWindowGhost(windowId);
      else this.showDetachedWindow(windowId);
      return;
    }
    if (placement.is_minimized) this.restoreWindow(windowId);
    else if (activeFocusedWindowId(this.state) === windowId) this.minimizeWindow(windowId);
    else this.raiseWindow(windowId);
  }

  refreshWindow(windowId: string): void {
    this.pageDriver?.reload(windowId);
  }

  /** A page reported where it is; posted to the window's location route when it differs from the stored
   *  record (this client's own for an independent window), and the record the route answers is taken at once,
   *  so the stored path is the reported one before the broadcast lands (a broadcast from another cause
   *  meanwhile must not read the report as a move to follow). Answers whether the shell took the report (or had
   *  nothing to take); false when it refused. */
  async reportLocation(windowId: string, path: string, title: string): Promise<boolean> {
    const found = findWindow(this.state, windowId);
    if (found === null) return false;
    const seen = effectiveWindow(this.state, found.window);
    if (seen.path === path && seen.title === title) return true;
    this.latestReportedPaths.set(windowId, path);
    let reported: WindowRecord;
    try {
      reported = await this.deps.api.reportWindowLocation(found.desktop.id, windowId, this.deps.clientId, path, title);
    } catch (error) {
      console.warn(`[si] the shell did not take the location of ${windowId}`, error);
      return false;
    }
    if (this.latestReportedPaths.get(windowId) !== path) return true;
    this.latestReportedPaths.delete(windowId);
    this.dispatch({ type: "window_location_reported", desktopId: found.desktop.id, window: reported });
    return true;
  }

  async quitApp(appName: string): Promise<void> {
    try {
      await this.deps.api.quitApp(appName);
    } catch (error) {
      this.toast(`Failed to quit ${appName}: ${(error as Error).message}`);
    }
  }

  openLauncher(): void {
    if (this.isLauncherOpenNow) return;
    this.isLauncherOpenNow = true;
    this.notifyListeners();
  }

  closeLauncher(): void {
    if (!this.isLauncherOpenNow) return;
    this.isLauncherOpenNow = false;
    this.notifyListeners();
  }

  /** A drag of a window's title bar began (past the threshold) at ``pointer``, in backdrop pixels. */
  beginWindowMove(windowId: string, pointer: PixelPoint): void {
    const placement = placementOf(this.state.layout, windowId);
    const startRect = this.renderedRect(placement);
    // A solo shell shows one window edge to edge; there is no desktop to pull a window out of.
    const isWatched = this.canPopOut && this.soloWindowId === null;
    this.releasedWatchedDrag = null;
    this.gesture = {
      kind: "move",
      windowId,
      startRect,
      startPointer: pointer,
      currentRect: startRect,
      zone: null,
      isUnsnapped: false,
      isWatched,
      isTearingOut: false,
    };
    this.raiseWindow(windowId);
    if (isWatched) this.announceWindowDrag(windowId, startRect, pointer);
    this.notifyListeners();
  }

  /** The pointer moved during a window drag. The gesture's rectangle and zone are updated with no
   *  redraw (a redraw per pointer event re-renders the whole desktop and repositions every live
   *  page): the App paints them straight onto the window, its page, and the snap preview, and
   *  ``gestureRectFor`` and ``snapPreviewRect`` keep answering the live values, so a redraw from
   *  any other cause mid-drag renders the same thing. */
  updateWindowMove(pointer: PixelPoint): void {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind !== "move") return;
    const placement = placementOf(this.state.layout, gesture.windowId);
    let start = gesture;
    if (placement.state !== "NORMAL" && !gesture.isUnsnapped) {
      const distance = Math.hypot(pointer.x - gesture.startPointer.x, pointer.y - gesture.startPointer.y);
      if (distance < this.metrics.unsnapDistance) return;
      // Un-snap: the kept frame's size hung under the pointer at the grab fraction, from here on a plain move.
      const grabFraction = (gesture.startPointer.x - gesture.startRect.x) / Math.max(gesture.startRect.width, 1);
      const grabOffsetY = (gesture.startPointer.y - gesture.startRect.y) / Math.max(this.backdrop.height, 1);
      const pointerFraction = {
        x: pointer.x / Math.max(this.backdrop.width, 1),
        y: pointer.y / Math.max(this.backdrop.height, 1),
      };
      const frame = unsnapFrame(placement.frame, pointerFraction, grabFraction, grabOffsetY);
      const unsnappedRect = fitFrameToBackdrop(frame, this.backdrop, this.metrics);
      start = { ...gesture, startRect: unsnappedRect, startPointer: pointer, isUnsnapped: true };
      // The window the chrome would pull out is a different size now, held elsewhere.
      if (start.isWatched) this.announceWindowDrag(start.windowId, unsnappedRect, pointer);
    }
    const delta = { x: pointer.x - start.startPointer.x, y: pointer.y - start.startPointer.y };
    const currentRect = movedRect(start.startRect, delta, this.backdrop, this.metrics);
    // While the chrome has the window out, no zone is offered; whether it is out is the chrome's word
    // (``setTearOut``), since the pointer events here stop at the chrome window's edge on some platforms.
    const zone = start.isTearingOut ? null : snapZoneForRelease(pointer, this.backdrop, this.metrics.snapThreshold);
    this.gesture = { ...start, currentRect, zone };
  }

  endWindowMove(pointer: PixelPoint): void {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind !== "move") return;
    this.updateWindowMove(pointer);
    const settled = this.gesture;
    this.gesture = null;
    if (settled === null || settled.kind !== "move") return;
    const placement = placementOf(this.state.layout, settled.windowId);
    if (settled.isWatched) {
      this.popOut.endWindowDrag(settled.windowId, settled.isTearingOut, false);
      this.releasedWatchedDrag = {
        windowId: settled.windowId,
        isDetached: settled.isTearingOut,
        placementAtStart: placement,
      };
    }
    if (settled.isTearingOut) {
      // Released outside, and this shell saw the release itself (a pointer that does leave the chrome's window):
      // the window was detached and saved as it went out, so the gesture only ends.
    } else if (settled.zone !== null) {
      // The frame is untouched so restore returns to it (an un-snapped drag kept it too).
      this.dispatch({ type: "window_state_set", windowId: settled.windowId, state: settled.zone });
    } else if (placement.state === "NORMAL" || settled.isUnsnapped) {
      this.dispatch({
        type: "window_frame_set",
        windowId: settled.windowId,
        frame: frameFromPixels(settled.currentRect, this.backdrop),
      });
    }
    this.notifyListeners();
  }

  beginWindowResize(windowId: string, edge: ResizeEdge): void {
    const placement = placementOf(this.state.layout, windowId);
    // Resizing a snapped or maximized window first un-snaps it, at the rectangle it rendered at.
    const startRect = this.renderedRect(placement);
    this.gesture = { kind: "resize", windowId, edge, startRect, currentRect: startRect };
    if (placement.state !== "NORMAL") {
      this.dispatch({ type: "window_frame_set", windowId, frame: frameFromPixels(startRect, this.backdrop) });
    } else {
      this.raiseWindow(windowId);
    }
    this.notifyListeners();
  }

  /** The pointer moved during a resize; no redraw, as for a move. */
  updateWindowResize(delta: PixelPoint): void {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind !== "resize") return;
    this.gesture = { ...gesture, currentRect: resizedRect(gesture.startRect, gesture.edge, delta, this.metrics) };
  }

  endWindowResize(delta: PixelPoint): void {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind !== "resize") return;
    this.updateWindowResize(delta);
    const settled = this.gesture;
    this.gesture = null;
    if (settled === null || settled.kind !== "resize") return;
    this.dispatch({
      type: "window_frame_set",
      windowId: settled.windowId,
      frame: frameFromPixels(settled.currentRect, this.backdrop),
    });
    this.notifyListeners();
  }

  /** A shortcut's icon was lifted out of the cell it is drawn in. */
  beginShortcutDrag(app: string, launch: string, pointer: PixelPoint): void {
    const key = shortcutKey(app, launch);
    const origin = this.placedShortcuts().find((entry) => placedShortcutKey(entry) === key);
    if (origin === undefined) return;
    this.gesture = {
      kind: "shortcut",
      app,
      launch,
      originCell: origin.cell,
      lift: { x: 0, y: 0 },
      targetCell: cellAtPoint(pointer, this.metrics, this.gridDimensions()),
    };
    this.notifyListeners();
  }

  /** The pointer moved during a shortcut drag. Only a new cell under it redraws: the room made is the same
   *  until then, and the lift itself is painted straight onto the icon (``paintShortcut``), a frame at a time. */
  updateShortcutDrag(pointer: PixelPoint, lift: PixelPoint): void {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind !== "shortcut") return;
    const targetCell = cellAtPoint(pointer, this.metrics, this.gridDimensions());
    const isSameTarget = isSameCell(targetCell, gesture.targetCell);
    this.gesture = { ...gesture, lift, targetCell };
    if (!isSameTarget) this.notifyListeners();
  }

  endShortcutDrag(pointer: PixelPoint, lift: PixelPoint): void {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind !== "shortcut") return;
    this.updateShortcutDrag(pointer, lift);
    const settled = this.gesture;
    this.gesture = null;
    // The arrangement goes in BEFORE the redraw: with the gesture gone and the shortcuts still recorded
    // in the cells they were in, a redraw here would put them both back for a frame.
    if (settled !== null && settled.kind === "shortcut") {
      void this.dropShortcut(settled.app, settled.launch, settled.targetCell, this.shortcutArrangement(settled));
    }
    this.notifyListeners();
  }

  /** Where the shortcuts sit right now: their placement over the current grid, with the room made for the
   *  one in the hand while a drag holds it over a cell. What the backdrop draws and what a drop commits. */
  placedShortcuts(): readonly PlacedShortcut[] {
    const gesture = this.gesture;
    if (gesture !== null && gesture.kind === "shortcut") return this.shortcutArrangement(gesture);
    return this.restingShortcuts();
  }

  /** Where the shortcuts sit with nothing in the hand: their placement over the current grid. */
  private restingShortcuts(): readonly PlacedShortcut[] {
    const desktop = activeDesktop(this.state);
    if (desktop === null) return [];
    return placeShortcuts(desktop.shortcuts, this.gridDimensions());
  }

  /** The box of the cell a shortcut is placed in, or null when the desktop holds no such shortcut: what the
   *  paint of a drop writes, as ``windowRect`` is for a window. For the one a drag holds this is the cell it
   *  would land in rather than the box it draws in, which its lift carries it out of. */
  shortcutRect(app: string, launch: string): PixelRect | null {
    const key = shortcutKey(app, launch);
    const entry = this.placedShortcuts().find((candidate) => placedShortcutKey(candidate) === key);
    return entry === undefined ? null : cellRect(entry.cell, this.metrics);
  }

  /** The arrangement ``gesture`` has made: the placement with the held shortcut in the cell under the pointer
   *  and the shortcut that was in it stepped aside. */
  private shortcutArrangement(gesture: ShortcutGesture): readonly PlacedShortcut[] {
    return withRoomMadeFor(
      this.restingShortcuts(),
      shortcutKey(gesture.app, gesture.launch),
      gesture.targetCell,
      this.gridDimensions(),
    );
  }

  /** A floating entry was lifted; ``grabOffset`` is where inside its box the pointer pressed. Nothing moves for
   *  an app with no pinned window on the active desktop. */
  beginFloatingEntryDrag(app: string, pointer: PixelPoint, grabOffset: PixelPoint): void {
    const current = this.presentationOf(app);
    if (current === null) return;
    const start = this.renderedFloatingEntryRect(app, current.position);
    this.gesture = { kind: "floating-entry", app, grabOffset, currentRect: start };
    this.updateFloatingEntryDrag(pointer);
  }

  /** The pointer moved during a floating entry drag; no redraw, as for a window move: the App paints the
   *  entry's rectangle straight onto it, and ``renderedFloatingEntryRect`` answers the live value meanwhile. */
  updateFloatingEntryDrag(pointer: PixelPoint): void {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind !== "floating-entry") return;
    const corner = { x: pointer.x - gesture.grabOffset.x, y: pointer.y - gesture.grabOffset.y };
    const clamped = floatingPositionFromPixels(corner, this.backdrop, this.metrics);
    this.gesture = { ...gesture, currentRect: floatingEntryRect(clamped, this.backdrop, this.metrics) };
  }

  /** The rectangle the desktop draws a floating entry at now, from the app alone: the drag's while one moves it,
   *  else its stored position's. What the paint of a drag writes onto the entry per pointer move and when the
   *  gesture ends, as ``windowRect`` is for a window. */
  floatingEntryRectOf(app: string): PixelRect {
    return this.renderedFloatingEntryRect(app, this.presentationOf(app)?.position ?? null);
  }

  /** The drag ended: the position is written to the client record, once. */
  endFloatingEntryDrag(pointer: PixelPoint): void {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind !== "floating-entry") return;
    this.updateFloatingEntryDrag(pointer);
    const settled = this.gesture;
    this.gesture = null;
    this.notifyListeners();
    if (settled === null || settled.kind !== "floating-entry") return;
    const current = this.presentationOf(settled.app);
    if (current === null) return;
    const position = floatingPositionFromPixels(settled.currentRect, this.backdrop, this.metrics);
    void this.setEntryPresentation(settled.app, { ...current, position });
  }

  cancelGesture(): void {
    if (this.gesture === null) return;
    const cancelled = this.gesture;
    this.gesture = null;
    // A watched drag cancelled mid-way (Escape, the browser): the chrome drops any window it was dragging, and
    // one that was out comes back to the desktop.
    if (cancelled.kind === "move" && cancelled.isWatched) {
      this.popOut.endWindowDrag(cancelled.windowId, false, true);
      if (cancelled.isTearingOut) this.bringBackDraggedWindow(cancelled.windowId);
    }
    this.notifyListeners();
  }

  private scheduleSave(): void {
    if (this.saveTimer !== null) clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => {
      this.saveTimer = null;
      void this.save();
    }, SAVE_DEBOUNCE_MS);
  }

  /** Save now if a gesture is waiting, and wait for any save already on its way. */
  async flushPendingSave(): Promise<void> {
    if (this.saveTimer !== null) {
      clearTimeout(this.saveTimer);
      this.saveTimer = null;
    }
    await this.save();
  }

  private save(): Promise<void> {
    if (this.saveInFlight !== null) return this.saveInFlight.then(() => this.save());
    if (!isLayoutDirty(this.state) || !this.state.isLayoutLoaded || this.state.activeDesktopId === null) {
      return Promise.resolve();
    }
    const desktopId = this.state.activeDesktopId;
    const version = this.state.layoutVersion;
    const request: PlacementsSaveRequest = {
      clientId: this.deps.clientId,
      saveId: this.saveIds.mint(),
      baseUpdatedAt: this.state.layout.updated_at,
      placements: this.state.layout.placements,
    };
    const inFlight = this.deps.api
      .savePlacements(desktopId, request)
      .then((updatedAt) => {
        this.dispatch({ type: "layout_saved", desktopId, version, updatedAt });
      })
      .catch((error: unknown) => {
        if (error instanceof StalePlacementsSaveError) {
          // The shell holds a newer layout (an agent op, another window's save): it wins, and the
          // gestures this window had not saved yet go with it.
          console.warn(`[si] the layout of ${desktopId} moved under this window; taking the shell's`, error);
          return this.refetchLayout();
        }
        console.warn(`[si] could not save the layout of ${desktopId}`, error);
      })
      .finally(() => {
        this.saveInFlight = null;
      });
    this.saveInFlight = inFlight;
    return inFlight;
  }

  /** Take the shell's layout of the active desktop when it differs from the one this window holds. */
  async refetchLayout(): Promise<void> {
    const desktopId = this.state.activeDesktopId;
    if (desktopId === null) return;
    const sequence = ++this.layoutFetchSequence;
    let layout: Layout;
    try {
      layout = await this.deps.api.fetchPlacements(desktopId, this.deps.clientId);
    } catch (error) {
      console.warn(`[si] could not fetch the layout of ${desktopId}`, error);
      return;
    }
    if (sequence !== this.layoutFetchSequence || desktopId !== this.state.activeDesktopId) return;
    // An unchanged stamp says the placements file did not move (this window's own save, or a broadcast that
    // carried nothing new for it): only the client's window paths, which change without moving the stamp, are
    // taken, and a gesture still waiting to be saved is kept rather than thrown away with a reload.
    if (this.state.isLayoutLoaded && layout.updated_at === this.state.layout.updated_at) {
      this.layoutLoadsRevision += 1;
      if (isSameWindowPaths(layout.window_paths, this.state.layout.window_paths)) {
        // Nothing to take, but the pages still follow: the shell handing back the paths this window already
        // holds is what confirms a page's own location report, so the page stops guarding the path it left.
        this.notifyListeners();
        return;
      }
      this.dispatch({ type: "window_paths_loaded", desktopId, windowPaths: layout.window_paths });
      return;
    }
    this.layoutLoadsRevision += 1;
    this.dispatch({ type: "layout_loaded", desktopId, layout });
    this.healSoloWindowOnFirstLoad();
  }

  /** A solo shell exists because its window is pulled out; the first layout it loads may say otherwise. A
   *  desktop window the chrome reopened (a relaunch, a reopen of the app, a backend retry) takes the layout as the
   *  truth at once: the window was brought back while it was away, and the report without it closes this
   *  window. A freshly torn-out one cannot: during a drag the desktop's shell writes the detach as the window
   *  goes out and that save is on its way while this shell boots, so a load that does not yet say it waits for
   *  the desktop's word (the broadcast's refetch is another load, which settles this). Only when no word comes
   *  within the grace (a detach whose save was refused) does this shell take its own existence as the truth
   *  and detach the window itself. Until then the report is held: one without the solo window would close this
   *  window. A later load that says the window is back is the desktop's word, and the chrome closes it. */
  private healSoloWindowOnFirstLoad(): void {
    if (!this.isSoloFirstLayoutPending || this.soloWindowId === null) return;
    const found = findWindow(this.state, this.soloWindowId);
    const isAttachedHere =
      found !== null &&
      found.desktop.id === this.state.activeDesktopId &&
      !placementOf(this.state.layout, this.soloWindowId).is_detached;
    if (!isAttachedHere || this.deps.isSoloReopened === true) {
      this.settleSoloFirstLoad();
      return;
    }
    if (this.soloHealTimer !== null) return;
    this.soloHealTimer = setTimeout(() => this.detachSoloWindowAfterGrace(), SOLO_HEAL_GRACE_MS);
  }

  /** The first load's question is answered (the layout says the window is out, or it is not here at all): the
   *  report the first load owed goes out. */
  private settleSoloFirstLoad(): void {
    this.isSoloFirstLayoutPending = false;
    if (this.soloHealTimer !== null) {
      clearTimeout(this.soloHealTimer);
      this.soloHealTimer = null;
    }
    this.reportDetachedWindows();
  }

  /** No load within the grace said the window is out: this shell's existence is the truth, and it writes it. */
  private detachSoloWindowAfterGrace(): void {
    this.soloHealTimer = null;
    if (!this.isSoloFirstLayoutPending || this.soloWindowId === null) return;
    this.isSoloFirstLayoutPending = false;
    this.dispatch({ type: "window_detached", windowId: this.soloWindowId });
    void this.flushPendingSave();
  }

  /** The frame a placement renders at while a gesture moves or resizes it, else its own. */
  gestureRectFor(windowId: string): PixelRect | null {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind === "shortcut" || gesture.kind === "floating-entry") return null;
    if (gesture.windowId !== windowId) return null;
    return gesture.currentRect;
  }

  /** The rectangle the desktop draws a window at now: the gesture's while one moves or resizes it, else
   *  its placement's. What a render positions the window by, and what the paint of a gesture writes
   *  onto it when the gesture ends or is cancelled, so the DOM already equals what the next render
   *  answers (a render diffs against the last render, not the DOM, and writes nothing it finds equal). */
  windowRect(windowId: string): PixelRect {
    return this.gestureRectFor(windowId) ?? this.renderedRect(placementOf(this.state.layout, windowId));
  }

  /** The rectangle the snap preview draws, or null when the drag is in no zone. */
  snapPreviewRect(): PixelRect | null {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind !== "move" || gesture.zone === null) return null;
    return frameToPixels(frameForState(MAXIMIZED_FRAME, gesture.zone), this.backdrop);
  }
}

/** What the notice that a visitor's desktop was deleted names: the deleted desktop, and the one the shell seeded
 *  in its place (not necessarily the one this client landed on: a deep link's desktop wins the landing). */
export interface ReplacedDesktop {
  readonly replacedName: string;
  readonly seededName: string;
}

/** The replaced desktop an arrival reports, when it does; the shell names the deleted desktop only alongside the
 *  one it seeded, so an answer with one but not the other reports nothing. */
export function replacedDesktopOf(arrival: ClientArrival | null): ReplacedDesktop | null {
  if (arrival === null || arrival.replaced_desktop_name === null || arrival.created_desktop === null) return null;
  return { replacedName: arrival.replaced_desktop_name, seededName: arrival.created_desktop.name };
}

/** The desktop a fresh window lands on: the deep link's when it exists, else the one the shell's arrival answer
 *  named when it exists, else the first; null with no desktops. */
export function chooseInitialDesktopId(
  desktops: readonly Desktop[],
  deepLinkDesktopId: string | null,
  arrivalDesktopId: string | null,
): string | null {
  if (desktops.length === 0) return null;
  const ids = new Set(desktops.map((desktop) => desktop.id));
  if (deepLinkDesktopId !== null && ids.has(deepLinkDesktopId)) return deepLinkDesktopId;
  if (arrivalDesktopId !== null && ids.has(arrivalDesktopId)) return arrivalDesktopId;
  return desktops[0].id;
}
