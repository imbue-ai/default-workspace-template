/**
 * ``DesktopStore``: the one class with mutable fields (desktop-interface plan section 6.2). It
 * holds this client's state (the record the reducers step), the theme metrics and the backdrop
 * size the geometry needs, the gesture in progress, and whether the launcher is open; applies
 * the reducers; schedules redraws; saves the layout with a debounce, a save id, and the stamp it
 * was based on (a stale save is refused with 409 and the layout refetched); and subscribes to
 * the socket. Everything that reads or writes the shell goes through here.
 */

import type {
  AppLifecycleAction,
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
  chatPath,
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
import { isSameWindowPaths } from "../model/records";
import { SaveIdMinter } from "../model/saveIds";
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
import { cellAtPoint, gridDimensions } from "../geometry/grid";
import type { GridDimensions } from "../geometry/grid";
import { placementOf } from "../geometry/stack";
import {
  activeDesktop,
  activeFocusedWindowId,
  appByName,
  chatApp,
  draftTargetOf,
  effectiveWindow,
  effectiveWindowTitle,
  entryLook,
  findWindow,
  initialDesktopState,
  isAppStoppable,
  isLayoutDirty,
  openableApps,
  pinnedWindowOf,
  reduceDesktopState,
  renderedState,
  windowShowingChat,
} from "../reducers/desktopState";
import type { DesktopEvent, DesktopState } from "../reducers/desktopState";
import { STILL_CONNECTING_NOTICE, cellForAddedShortcut, resolveLaunchRun } from "../reducers/shortcuts";
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
  setAppLifecycle(appName: string, action: AppLifecycleAction): Promise<void>;
  setEntryPresentation(clientId: string, app: string, presentation: EntryPresentation): Promise<ClientRecord>;
  fetchAvatars(): Promise<AvatarCatalog>;
  selectAvatar(design: string): Promise<void>;
}

/** What the live-page layer does for the store, registered by that layer (it sits above the store). */
export interface PageDriver {
  /** Reload one window's page. */
  reload(windowId: string): void;
  /** Reload every page of an app. */
  reloadApp(appName: string): void;
  /** Send the page ``shell:close-request`` (the minds close chord). */
  requestClose(windowId: string): void;
}

export interface StoreDependencies {
  readonly clientId: string;
  readonly api: DesktopApi;
  readonly socket: DesktopSocket;
  readonly metrics: ThemeMetrics;
  readonly modes: RenderModes;
  /** Schedule a redraw of the views after a state change. */
  readonly redraw: () => void;
  /** Tell the user about a refusal. */
  readonly notify: (message: string) => void;
  /** Reload the whole interface (the ``reload_system_interface`` op). */
  readonly reloadInterface: () => void;
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
  /** Where the lifted icon is drawn (the pointer, less the grab offset). */
  readonly iconPosition: PixelPoint;
  readonly grabOffset: PixelPoint;
  readonly targetCell: GridCell;
}

/** A floating entry being dragged: its box's top-left corner follows the pointer less the grab offset. */
export interface FloatingEntryGesture {
  readonly kind: "floating-entry";
  readonly app: string;
  readonly grabOffset: PixelPoint;
  readonly currentRect: PixelRect;
}

export type ActiveGesture = MoveGesture | ResizeGesture | ShortcutGesture | FloatingEntryGesture;

type Listener = () => void;

export class DesktopStore {
  private state: DesktopState;
  private metrics: ThemeMetrics;
  private backdrop: PixelSize = { width: 0, height: 0 };
  private gesture: ActiveGesture | null = null;
  private isLauncherOpenNow = false;
  // Set when the shell had to seed a fresh desktop for this user at arrival; the notice shows once.
  private replacedDesktop: ReplacedDesktop | null = null;
  private readonly listeners = new Set<Listener>();
  private readonly saveIds = new SaveIdMinter();
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

  constructor(private readonly deps: StoreDependencies) {
    this.state = initialDesktopState(deps.clientId, deps.modes);
    this.metrics = deps.metrics;
    this.appsLoaded = new Promise((resolve) => {
      this.markAppsLoaded = resolve;
    });
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

  /** The rectangle a placement renders at, in backdrop pixels (the compact override and the fit applied). */
  renderedRect(placement: Placement): PixelRect {
    return fitFrameToBackdrop(
      frameForState(placement.frame, renderedState(placement, this.state.modes)),
      this.backdrop,
      this.metrics,
    );
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => void this.listeners.delete(listener);
  }

  setPageDriver(driver: PageDriver | null): void {
    this.pageDriver = driver;
  }

  dispatch(event: DesktopEvent): void {
    const next = reduceDesktopState(this.state, event);
    if (next === this.state) return;
    // Only an event that changed the layout re-arms the debounce: a broadcast landing while a save
    // waits (every page's location report is one) must not push the save back.
    const isLayoutChanged = next.layoutVersion !== this.state.layoutVersion;
    this.state = next;
    if (isLayoutChanged && isLayoutDirty(next)) this.scheduleSave();
    this.notifyListeners();
  }

  private notifyListeners(): void {
    for (const listener of this.listeners) listener();
    this.deps.redraw();
  }

  setBackdropSize(size: PixelSize): void {
    if (size.width === this.backdrop.width && size.height === this.backdrop.height) return;
    this.backdrop = size;
    this.notifyListeners();
  }

  setThemeMetrics(metrics: ThemeMetrics, modes: RenderModes): void {
    this.metrics = metrics;
    // The modes event always yields a new state, so the dispatch notifies and redraws.
    this.dispatch({ type: "render_modes_changed", modes });
  }

  /** Resolves once the app list has landed, with the bootstrap's inventory read or the socket's first
   *  ``apps_updated``, whichever comes first. A ``start`` that failed to read the inventory resolves without
   *  it, so a caller that needs the apps (which app holds chats, which window is pinned) waits on this too. */
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
        this.dispatch({ type: "avatar_selection_updated", design, defaultDesign: null });
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
      this.deps.notify(`Could not read the desktops and apps: ${(error as Error).message}`);
      return;
    }
    // The apps before the desktops, so the first draw of a desktop's shortcuts already knows every app.
    this.takeApps(inventory.apps);
    this.desktopsRevision += 1;
    this.dispatch({ type: "desktops_updated", desktops: inventory.desktops });
    this.replacedDesktop = replacedDesktopOf(arrival);
    const own = inventory.clients.find((client) => client.id === this.deps.clientId);
    this.takeFetchedEntries(own, entryPushesBefore);
    // The shell's answer says where this client lands; without one (the arrival failed), the recorded desktop.
    const landing = arrival?.desktop_id ?? own?.active_desktop ?? null;
    const chosen = chooseInitialDesktopId(inventory.desktops, deepLink.desktopId, landing);
    if (chosen === null) return;
    await this.switchDesktop(chosen, { isFollowingPush: true });
    if (deepLink.open === null && deepLink.launch === null) return;
    await this.appsLoaded;
    await this.applyDeepLink(deepLink);
  }

  /** The socket (re)opened: the shell hears which desktop this client is on. The first connect leaves
   *  the rest to ``start``; a reconnect resynchronises, since the messages of the time apart are gone
   *  with the socket (the shell resends the apps and desktops itself). */
  private takeConnected(): void {
    if (this.hasSocketConnected) {
      void this.resyncAfterReconnect();
      return;
    }
    this.hasSocketConnected = true;
    this.reportClientState("");
  }

  /** The client record is the shell's word after a reconnect: another window of this client may have
   *  switched desktops meanwhile (the ``active_desktop_changed`` is gone), and reporting this window's
   *  own desktop would move the whole client back to it. So the recorded desktop is adopted as a push
   *  when it differs, and the layout is read again either way, for the ``placements_updated`` missed.
   *  The record's entries and the workspace's selection are taken again too, for the
   *  ``client_entries_changed`` and ``avatar_selection_changed`` missed (the server resends the rest). */
  private async resyncAfterReconnect(): Promise<void> {
    let recorded: string | null = null;
    const entryPushesBefore = this.entryPushes;
    try {
      const clients = await this.deps.api.fetchClients();
      const own = clients.find((client) => client.id === this.deps.clientId);
      this.takeFetchedEntries(own, entryPushesBefore);
      recorded = own?.active_desktop ?? null;
    } catch (error) {
      console.warn("[si] could not read the client records after reconnecting", error);
    }
    void this.loadAvatarSelection();
    const isRecordedKnown = recorded !== null && this.state.desktops.some((desktop) => desktop.id === recorded);
    if (recorded !== null && isRecordedKnown && recorded !== this.state.activeDesktopId) {
      await this.switchDesktop(recorded, { isFollowingPush: true });
      return;
    }
    this.reportClientState("");
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

  private reportClientState(previousDesktop: string): void {
    const active = this.state.activeDesktopId;
    if (active === null) return;
    this.deps.socket.reportClientState(active, previousDesktop);
  }

  private takeDesktops(desktops: Desktop[]): void {
    const previous = this.state.activeDesktopId;
    this.desktopsRevision += 1;
    this.dispatch({ type: "desktops_updated", desktops });
    if (this.state.activeDesktopId !== previous) {
      // The active desktop was deleted and the reducer landed on the fallback: this client follows as it
      // would a push, telling the shell and fetching the layout it now shows.
      this.cancelGesture();
      this.reportClientState("");
      void this.refetchLayout();
    }
  }

  private takePlacementsUpdated(event: PlacementsUpdatedEvent): void {
    if (event.clientId !== this.deps.clientId || event.desktopId !== this.state.activeDesktopId) return;
    if (this.saveIds.isOwn(event.saveId)) return;
    void this.refetchLayout();
  }

  private takeActiveDesktopChanged(event: ActiveDesktopChangedEvent): void {
    if (event.clientId !== this.deps.clientId || event.desktopId === this.state.activeDesktopId) return;
    void this.switchDesktop(event.desktopId, { isFollowingPush: true });
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
      this.dispatch({ type: "avatar_selection_updated", design, defaultDesign: catalog.default });
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

  /** ``minds:focus-chat`` from the embedder: show the chat ``chatId``. A window already showing it is
   *  switched to and raised, wherever it is; otherwise this client's view of the chat app's pinned
   *  window is pointed at the chat, as a draft is, so the chat lands where this viewer reads chats;
   *  with no pinned window to take it, the chat opens in a window of its own. False when nothing
   *  showed it -- this machine has no app that holds chats, or the shell refused the ask. */
  async focusChat(chatId: string): Promise<boolean> {
    const shown = windowShowingChat(this.state, chatId);
    if (shown !== null) {
      if (shown.desktop.id !== this.state.activeDesktopId) await this.switchDesktop(shown.desktop.id);
      this.restoreWindow(shown.window.id);
      return true;
    }
    const app = chatApp(this.state);
    if (app === null) return false;
    const pinned = pinnedWindowOf(this.state, app.name);
    if (pinned === null) return (await this.openWindowAt(app.name, chatPath(chatId), "focus")) !== null;
    const isTaken = await this.navigateOwnWindow(pinned.id, chatPath(chatId));
    this.restoreWindow(pinned.id);
    return isTaken;
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
      this.deps.notify(`Could not move the window: ${(error as Error).message}`);
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
      this.deps.notify(`Could not change the avatar: ${(error as Error).message}`);
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
      this.deps.notify(`Could not change the entry: ${(error as Error).message}`);
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
    }
  }

  /** Switch this client onto ``desktopId``: flush the outgoing layout's pending save, tell the shell,
   *  and fetch the incoming layout. A switch following a push (``active_desktop_changed``, the
   *  bootstrap) reports no previous desktop, since the shell already recorded it. */
  async switchDesktop(desktopId: string, options: { isFollowingPush?: boolean } = {}): Promise<void> {
    const previous = this.state.activeDesktopId;
    if (previous === desktopId) return;
    await this.flushPendingSave();
    this.cancelGesture();
    this.dispatch({ type: "desktop_activated", desktopId });
    this.reportClientState(options.isFollowingPush === true ? "" : (previous ?? ""));
    await this.refetchLayout();
  }

  async createDesktop(name: string, color: string, glyph: number): Promise<void> {
    try {
      const created = await this.deps.api.createDesktop(name, color, glyph);
      // The broadcast may have landed first: upsert rather than append.
      this.takeDesktop(created);
      await this.switchDesktop(created.id);
    } catch (error) {
      this.deps.notify(`Could not create the desktop: ${(error as Error).message}`);
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

  /** Add a launch path to the active desktop at the first free cell in reading order over the current grid;
   *  nothing when it is already there (the shell would move the shortcut and reset its mode). */
  async addShortcut(app: string, launch: string, mode: ShortcutMode): Promise<void> {
    const desktop = activeDesktop(this.state);
    if (desktop === null) return;
    if (desktop.shortcuts.some((shortcut) => shortcut.target.app === app && shortcut.target.launch === launch)) return;
    const cell = cellForAddedShortcut(desktop, this.gridDimensions());
    await this.setShortcut(desktop.id, { target: { kind: "launch", app, launch }, mode, cell });
  }

  async setShortcut(desktopId: string, shortcut: DesktopShortcut): Promise<void> {
    try {
      this.takeDesktop(await this.deps.api.setDesktopShortcut(desktopId, shortcut));
    } catch (error) {
      this.deps.notify(`Could not add the shortcut: ${(error as Error).message}`);
    }
  }

  async moveShortcut(app: string, launch: string, cell: GridCell): Promise<void> {
    const desktop = activeDesktop(this.state);
    if (desktop === null) return;
    try {
      this.takeDesktop(await this.deps.api.moveDesktopShortcut(desktop.id, app, launch, cell));
    } catch (error) {
      this.deps.notify(`Could not move the shortcut: ${(error as Error).message}`);
    }
  }

  async removeShortcut(app: string, launch: string): Promise<void> {
    const desktop = activeDesktop(this.state);
    if (desktop === null) return;
    try {
      this.takeDesktop(await this.deps.api.removeDesktopShortcut(desktop.id, app, launch));
    } catch (error) {
      this.deps.notify(`Could not remove the shortcut: ${(error as Error).message}`);
    }
  }

  /** Run a shortcut in its mode: focus raises the app's most recent window and opens only when there is none.
   *  Before the apps are known (the inventory has not answered) the user is told to wait rather than that
   *  the app is missing. */
  async runLaunch(app: string, launch: string, mode: ShortcutMode): Promise<void> {
    const run = resolveLaunchRun(this.state, app, launch, mode);
    switch (run.kind) {
      case "raise":
        this.raiseWindow(run.windowId);
        return;
      case "open":
        await this.launchAt(run.app, run.launch, {}, { kind: "new" });
        return;
      case "connecting":
        this.deps.notify(STILL_CONNECTING_NOTICE);
        return;
      case "unavailable":
        this.deps.notify(`Cannot open: ${run.reason}`);
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
      this.deps.notify(`Cannot open: ${appName} has no launch path ${launchId}`);
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
      this.deps.notify(disabledReason);
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
      this.deps.notify(NO_DRAFT_APP_REASON);
      return false;
    }
    return this.runFreeText(draftRow.app.name, draftRow.launchPath.id, text);
  }

  /** A page's ``shell:start-with-text`` (launcher plan section 3.7): the primary text action runs with the text;
   *  with no free-text row on the machine the user is told. */
  async startWithText(text: string): Promise<boolean> {
    const [primary] = freeTextRowsOf(openableApps(this.state));
    if (primary === undefined) {
      this.deps.notify(NO_TEXT_APP_REASON);
      return false;
    }
    return this.runFreeText(primary.app.name, primary.launchPath.id, text);
  }

  /** Every open goes through the shell's one route; the answer is applied at once and the layout
   *  refetched for the stamp the shell wrote. Answers the window id, or null when the shell refused. */
  async openWindowAt(app: string, path: string, ifPresent: IfPresent): Promise<string | null> {
    const desktopId = this.state.activeDesktopId;
    if (desktopId === null) return null;
    // The shell writes the new placement over the stored layout, and the refetch takes that: a gesture
    // still waiting in the debounce goes into the file first or it is lost.
    await this.flushPendingSave();
    let outcome: WindowOpenOutcome;
    try {
      outcome = await this.deps.api.openWindow(desktopId, { app, path, clientId: this.deps.clientId, ifPresent });
    } catch (error) {
      this.deps.notify(`Could not open ${app}: ${(error as Error).message}`);
      return null;
    }
    this.dispatch({ type: "window_opened_here", desktopId, window: outcome.window, isNew: outcome.isNew });
    void this.refetchLayout();
    return outcome.window.id;
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
    const desktopId = this.state.activeDesktopId;
    if (desktopId === null) return null;
    await this.flushPendingSave();
    let outcome: LaunchOutcome;
    try {
      outcome = await this.deps.api.launch(desktopId, {
        app,
        launch: launchId,
        params,
        clientId: this.deps.clientId,
        target,
      });
    } catch (error) {
      this.deps.notify(`Could not open ${app}: ${(error as Error).message}`);
      return null;
    }
    if (target.kind === "window") {
      const found = findWindow(this.state, target.windowId);
      if (found !== null) this.applyOwnNavigation(found, outcome.window);
      return outcome.window.id;
    }
    this.dispatch({ type: "window_opened_here", desktopId, window: outcome.window, isNew: outcome.isNew });
    void this.refetchLayout();
    return outcome.window.id;
  }

  /** ``shell:open {path, ifPresent}`` from a page: a window of the posting window's own app, on its desktop. */
  async openPathFromWindow(windowId: string, path: string, ifPresent: IfPresent): Promise<void> {
    const found = findWindow(this.state, windowId);
    if (found === null) return;
    if (found.desktop.id !== this.state.activeDesktopId) await this.switchDesktop(found.desktop.id);
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
      this.deps.notify(`Could not close the window: ${(error as Error).message}`);
      return;
    }
    this.dispatch({ type: "window_closed_here", desktopId: found.desktop.id, windowId });
  }

  /** The minds close chord: the focused window is told, then closed for everyone; a pinned window, which is never
   *  closed, is minimized instead. */
  async closeFocusedWindow(): Promise<void> {
    const focused = activeFocusedWindowId(this.state);
    if (focused === null) return;
    if (findWindow(this.state, focused)?.window.is_pinned === true) {
      this.minimizeWindow(focused);
      return;
    }
    this.pageDriver?.requestClose(focused);
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

  raiseWindow(windowId: string): void {
    this.dispatch({ type: "window_raised", windowId });
  }

  minimizeWindow(windowId: string): void {
    this.dispatch({ type: "window_minimized", windowId });
  }

  /** Restore a minimized window (the taskbar's verb): raised. */
  restoreWindow(windowId: string): void {
    this.raiseWindow(windowId);
  }

  setWindowState(windowId: string, state: WindowState): void {
    this.dispatch({ type: "window_state_set", windowId, state });
  }

  toggleMaximized(windowId: string): void {
    if (this.state.modes.isCompact) return;
    const placement = placementOf(this.state.layout, windowId);
    if (placement.state === "MAXIMIZED") this.dispatch({ type: "window_restored", windowId });
    else this.dispatch({ type: "window_state_set", windowId, state: "MAXIMIZED" });
  }

  /** A taskbar entry's click: restore and raise when minimized, minimize when focused, raise otherwise. */
  toggleTaskbarEntry(windowId: string): void {
    const placement = placementOf(this.state.layout, windowId);
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

  async setAppLifecycle(appName: string, action: AppLifecycleAction): Promise<void> {
    try {
      await this.deps.api.setAppLifecycle(appName, action);
    } catch (error) {
      this.deps.notify(`Failed to ${action} ${appName}: ${(error as Error).message}`);
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
    if (this.state.modes.isCompact) return;
    const placement = placementOf(this.state.layout, windowId);
    const startRect = this.renderedRect(placement);
    this.raiseWindow(windowId);
    this.gesture = {
      kind: "move",
      windowId,
      startRect,
      startPointer: pointer,
      currentRect: startRect,
      zone: null,
      isUnsnapped: false,
    };
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
    }
    const delta = { x: pointer.x - start.startPointer.x, y: pointer.y - start.startPointer.y };
    const currentRect = movedRect(start.startRect, delta, this.backdrop, this.metrics);
    const zone = snapZoneForRelease(pointer, this.backdrop, this.metrics.snapThreshold);
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
    if (settled.zone !== null) {
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
    if (this.state.modes.isCompact) return;
    const placement = placementOf(this.state.layout, windowId);
    // Resizing a snapped or maximized window first un-snaps it, at the rectangle it rendered at.
    const startRect = this.renderedRect(placement);
    if (placement.state !== "NORMAL") {
      this.dispatch({ type: "window_frame_set", windowId, frame: frameFromPixels(startRect, this.backdrop) });
    } else {
      this.raiseWindow(windowId);
    }
    this.gesture = { kind: "resize", windowId, edge, startRect, currentRect: startRect };
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

  /** A shortcut's icon was lifted; ``grabOffset`` is where inside its cell the pointer pressed. */
  beginShortcutDrag(app: string, launch: string, pointer: PixelPoint, grabOffset: PixelPoint): void {
    this.gesture = {
      kind: "shortcut",
      app,
      launch,
      grabOffset,
      iconPosition: { x: pointer.x - grabOffset.x, y: pointer.y - grabOffset.y },
      targetCell: cellAtPoint(pointer, this.metrics, this.gridDimensions()),
    };
    this.notifyListeners();
  }

  updateShortcutDrag(pointer: PixelPoint): void {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind !== "shortcut") return;
    this.gesture = {
      ...gesture,
      iconPosition: { x: pointer.x - gesture.grabOffset.x, y: pointer.y - gesture.grabOffset.y },
      targetCell: cellAtPoint(pointer, this.metrics, this.gridDimensions()),
    };
    this.notifyListeners();
  }

  endShortcutDrag(pointer: PixelPoint): void {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind !== "shortcut") return;
    this.updateShortcutDrag(pointer);
    const settled = this.gesture;
    this.gesture = null;
    this.notifyListeners();
    if (settled === null || settled.kind !== "shortcut") return;
    void this.moveShortcut(settled.app, settled.launch, settled.targetCell);
  }

  /** A floating entry was lifted; ``grabOffset`` is where inside its box the pointer pressed. Nothing moves
   *  in compact mode, or for an app with no pinned window on the active desktop. */
  beginFloatingEntryDrag(app: string, pointer: PixelPoint, grabOffset: PixelPoint): void {
    if (this.state.modes.isCompact) return;
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
    this.gesture = null;
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
      if (isSameWindowPaths(layout.window_paths, this.state.layout.window_paths)) return;
      this.layoutLoadsRevision += 1;
      this.dispatch({ type: "window_paths_loaded", desktopId, windowPaths: layout.window_paths });
      return;
    }
    this.layoutLoadsRevision += 1;
    this.dispatch({ type: "layout_loaded", desktopId, layout });
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
