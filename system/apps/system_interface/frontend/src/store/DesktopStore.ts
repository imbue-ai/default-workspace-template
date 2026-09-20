/**
 * ``DesktopStore``: the one class with mutable fields (desktop-interface plan section 6.2). It
 * holds this client's state (the record the reducers step), the theme metrics and the backdrop
 * size the geometry needs, the gesture in progress, and whether the launcher is open; applies
 * the reducers; schedules redraws; saves the layout with a debounce, a save id, and the stamp it
 * was based on (a stale save is refused with 409 and the layout refetched); and subscribes to
 * the socket. Everything that reads or writes the shell goes through here.
 */

import type { AppLifecycleAction, PlacementsSaveRequest, WindowOpenOutcome, WindowOpenRequest } from "../model/api";
import { StalePlacementsSaveError } from "../model/api";
import { launchPathOf, launchPathWithParams } from "../model/launch";
import type {
  AppRecord,
  Desktop,
  DesktopShortcut,
  GridCell,
  IfPresent,
  Layout,
  Placement,
  SharingMode,
  ShortcutMode,
  Wallpaper,
  WindowRecord,
  WindowState,
} from "../model/records";
import { SaveIdMinter } from "../model/saveIds";
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
import { cellAtPoint, gridDimensions } from "../geometry/grid";
import type { GridDimensions } from "../geometry/grid";
import { placementOf } from "../geometry/stack";
import {
  activeDesktop,
  activeFocusedWindowId,
  appByName,
  findWindow,
  initialDesktopState,
  isAppStoppable,
  isLayoutDirty,
  reduceDesktopState,
  renderedState,
} from "../reducers/desktopState";
import type { DesktopEvent, DesktopState } from "../reducers/desktopState";
import { cellForAddedShortcut, resolveLaunchRun } from "../reducers/shortcuts";
import type { ThemeMetrics, RenderModes } from "../theme/metrics";
import type { ActiveDesktopChangedEvent, DesktopSocket, LayoutOpEvent, PlacementsUpdatedEvent } from "./socket";

// A gesture's save lands shortly after it ends; the shell edits the saved layout for agent ops, and
// an op that follows a gesture has to see the gesture in the file.
const SAVE_DEBOUNCE_MS = 300;

/** The routes the store calls, injectable so the store is tested against a fake shell. */
export interface DesktopApi {
  fetchDesktops(): Promise<Desktop[]>;
  createDesktop(name: string, color: string, glyph: number): Promise<Desktop>;
  updateDesktopSettings(
    desktopId: string,
    name: string,
    color: string,
    glyph: number,
    sharing: SharingMode,
  ): Promise<Desktop>;
  setDesktopWallpaper(desktopId: string, wallpaper: Wallpaper | null): Promise<Desktop>;
  deleteDesktop(desktopId: string): Promise<string>;
  setDesktopShortcut(desktopId: string, shortcut: DesktopShortcut): Promise<Desktop>;
  moveDesktopShortcut(desktopId: string, app: string, launch: string, cell: GridCell): Promise<Desktop>;
  removeDesktopShortcut(desktopId: string, app: string, launch: string): Promise<Desktop>;
  openWindow(desktopId: string, request: WindowOpenRequest): Promise<WindowOpenOutcome>;
  closeWindow(desktopId: string, windowId: string): Promise<void>;
  reportWindowLocation(desktopId: string, windowId: string, path: string, title: string): Promise<WindowRecord>;
  fetchPlacements(desktopId: string, clientId: string): Promise<Layout>;
  savePlacements(desktopId: string, request: PlacementsSaveRequest): Promise<string | null>;
  fetchClients(): Promise<{ id: string; active_desktop: string | null }[]>;
  setAppLifecycle(appName: string, action: AppLifecycleAction): Promise<void>;
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

export type ActiveGesture = MoveGesture | ResizeGesture | ShortcutGesture;

type Listener = () => void;

export class DesktopStore {
  private state: DesktopState;
  private metrics: ThemeMetrics;
  private backdrop: PixelSize = { width: 0, height: 0 };
  private gesture: ActiveGesture | null = null;
  private isLauncherOpenNow = false;
  private readonly listeners = new Set<Listener>();
  private readonly saveIds = new SaveIdMinter();
  private readonly pendingRestores = new Set<string>();
  private saveTimer: ReturnType<typeof setTimeout> | null = null;
  private saveInFlight: Promise<void> | null = null;
  private layoutFetchSequence = 0;
  private pageDriver: PageDriver | null = null;
  private hasSocketConnected = false;
  // The path each window's page reported last, so a report answered out of order is not applied.
  private readonly latestReportedPaths = new Map<string, string>();
  // Bumped by every desktops record the shell hands over (the bootstrap's read, each broadcast), and
  // not by a local edit: the live pages follow their windows' stored paths after the shell speaks.
  private desktopsRevision = 0;
  // The app list arrives only over the socket, so a deep link's open or launch waits for it here.
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

  /** Whether this client's layout holds a placement for the window. While a window settles, only the
   *  client that opened it has one (the shell places the requesting client's layout at open, and every
   *  other client defers its gestures on the window), so this tells the opener from the rest. */
  isPlacedHere(windowId: string): boolean {
    return this.state.layout.placements.some((placement) => placement.window_id === windowId);
  }

  gridDimensions(): GridDimensions {
    return gridDimensions(this.backdrop, this.metrics);
  }

  /** Whether the workspace can stop and start the app (supervised, not critical, not inside a critical program). */
  canStopApp(app: AppRecord): boolean {
    return isAppStoppable(this.state, app);
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

  /** Read this client's record, pick the desktop (a deep link's first), connect, fetch the layout, and
   *  honour the deep link's open or launch. */
  async start(deepLink: DeepLink): Promise<void> {
    this.deps.socket.connect({
      onConnected: () => this.takeConnected(),
      onAppsUpdated: (apps) => this.takeApps(apps),
      onDesktopsUpdated: (desktops) => this.takeDesktops(desktops),
      onPlacementsUpdated: (event) => this.takePlacementsUpdated(event),
      onActiveDesktopChanged: (event) => this.takeActiveDesktopChanged(event),
      onLayoutOp: (event) => this.handleLayoutOp(event),
    });
    let desktops: Desktop[];
    let clients: Awaited<ReturnType<DesktopApi["fetchClients"]>>;
    try {
      [desktops, clients] = await Promise.all([
        this.deps.api.fetchDesktops(),
        this.deps.api.fetchClients().catch((error: unknown) => {
          console.warn("[si] could not read the client records", error);
          return [];
        }),
      ]);
    } catch (error) {
      console.warn("[si] could not read the desktops", error);
      this.deps.notify(`Could not read the desktops: ${(error as Error).message}`);
      return;
    }
    this.desktopsRevision += 1;
    this.dispatch({ type: "desktops_updated", desktops });
    const recorded = clients.find((client) => client.id === this.deps.clientId)?.active_desktop ?? null;
    const chosen = chooseInitialDesktopId(desktops, deepLink.desktopId, recorded);
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
   *  when it differs, and the layout is read again either way, for the ``placements_updated`` missed. */
  private async resyncAfterReconnect(): Promise<void> {
    let recorded: string | null = null;
    try {
      const clients = await this.deps.api.fetchClients();
      recorded = clients.find((client) => client.id === this.deps.clientId)?.active_desktop ?? null;
    } catch (error) {
      console.warn("[si] could not read the client records after reconnecting", error);
    }
    const isRecordedKnown = recorded !== null && this.state.desktops.some((desktop) => desktop.id === recorded);
    if (recorded !== null && isRecordedKnown && recorded !== this.state.activeDesktopId) {
      await this.switchDesktop(recorded, { isFollowingPush: true });
      return;
    }
    this.reportClientState("");
    await this.refetchLayout();
  }

  private takeApps(apps: AppRecord[]): void {
    this.dispatch({ type: "apps_updated", apps });
    this.markAppsLoaded();
  }

  private async applyDeepLink(link: DeepLink): Promise<void> {
    if (link.open !== null) {
      if (appByName(this.state, link.open.app) === undefined)
        console.warn(`[si] deep link ignored: no app ${link.open.app}`);
      else await this.openWindowAt(link.open.app, link.open.path, null, "focus");
    }
    if (link.launch !== null) {
      const app = appByName(this.state, link.launch.app);
      const launchPath = app === undefined ? null : launchPathOf(app, link.launch.launch);
      if (app === undefined || launchPath === null) {
        console.warn(`[si] deep link ignored: no launch path ${link.launch.app}:${link.launch.launch}`);
      } else {
        await this.openWindowAt(app.name, launchPath.path, launchPath.id, "new");
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
      this.pendingRestores.clear();
      this.reportClientState("");
      void this.refetchLayout();
      return;
    }
    // A restore deferred while its window settled runs once the window has a real path.
    const desktop = activeDesktop(this.state);
    if (desktop === null) return;
    for (const windowId of [...this.pendingRestores]) {
      const window = desktop.windows.find((candidate) => candidate.id === windowId);
      if (window === undefined) this.pendingRestores.delete(windowId);
      else if (!window.is_settling) {
        this.pendingRestores.delete(windowId);
        this.dispatch({ type: "window_raised", windowId });
      }
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
    // A restore deferred on the desktop being left is not owed to the user when they come back.
    this.pendingRestores.clear();
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

  async updateDesktopSettings(
    desktopId: string,
    name: string,
    color: string,
    glyph: number,
    sharing: SharingMode,
  ): Promise<void> {
    this.takeDesktop(await this.deps.api.updateDesktopSettings(desktopId, name, color, glyph, sharing));
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

  /** Run a shortcut in its mode: focus raises the app's most recent window and opens only when there is none. */
  async runLaunch(app: string, launch: string, mode: ShortcutMode): Promise<void> {
    const run = resolveLaunchRun(this.state, app, launch, mode);
    switch (run.kind) {
      case "raise":
        this.dispatch({ type: "window_raised", windowId: run.windowId });
        return;
      case "open":
        await this.openWindowAt(run.app, run.path, run.launch, "new");
        return;
      case "unavailable":
        this.deps.notify(`Cannot open: ${run.reason}`);
        return;
    }
  }

  runShortcut(shortcut: DesktopShortcut): Promise<void> {
    return this.runLaunch(shortcut.target.app, shortcut.target.launch, shortcut.mode);
  }

  /** Run a launch path with params (a seeded prompt): always opens a new window. */
  async openLaunchPath(app: string, launch: string, params: Readonly<Record<string, string>>): Promise<void> {
    const record = appByName(this.state, app);
    const launchPath = record === undefined ? null : launchPathOf(record, launch);
    if (record === undefined || launchPath === null) {
      this.deps.notify(`Cannot open: ${app} has no launch path ${launch}`);
      return;
    }
    await this.openWindowAt(record.name, launchPathWithParams(launchPath, params), launchPath.id, "new");
  }

  /** Every open goes through the shell's one route; the answer is applied at once and the layout
   *  refetched for the stamp the shell wrote. Answers the window id, or null when the shell refused. */
  async openWindowAt(app: string, path: string, launch: string | null, ifPresent: IfPresent): Promise<string | null> {
    const desktopId = this.state.activeDesktopId;
    if (desktopId === null) return null;
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
        launch,
      });
    } catch (error) {
      this.deps.notify(`Could not open ${app}: ${(error as Error).message}`);
      return null;
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
    await this.openWindowAt(found.window.app, path, null, ifPresent);
  }

  async closeWindow(windowId: string): Promise<void> {
    const found = findWindow(this.state, windowId);
    if (found === null) return;
    try {
      await this.deps.api.closeWindow(found.desktop.id, windowId);
    } catch (error) {
      this.deps.notify(`Could not close the window: ${(error as Error).message}`);
      return;
    }
    this.pendingRestores.delete(windowId);
    this.dispatch({ type: "window_closed_here", desktopId: found.desktop.id, windowId });
  }

  /** The minds close chord: the focused window is told, then closed for everyone. */
  async closeFocusedWindow(): Promise<void> {
    const focused = activeFocusedWindowId(this.state);
    if (focused === null) return;
    this.pageDriver?.requestClose(focused);
    await this.closeWindow(focused);
  }

  /** Whether showing the window has to wait: it is still settling on another client's open, and this
   *  client has no placement for it. Showing it now would create its page at the launch path and run
   *  the launch a second time, so the restore is queued for when the window has a real path. */
  private deferWhileSettling(windowId: string): boolean {
    const found = findWindow(this.state, windowId);
    if (found === null || !found.window.is_settling || this.isPlacedHere(windowId)) return false;
    this.pendingRestores.add(windowId);
    return true;
  }

  raiseWindow(windowId: string): void {
    if (this.deferWhileSettling(windowId)) return;
    this.dispatch({ type: "window_raised", windowId });
  }

  minimizeWindow(windowId: string): void {
    this.dispatch({ type: "window_minimized", windowId });
  }

  /** Restore a minimized window (the taskbar's verb): raised, and deferred while it settles elsewhere. */
  restoreWindow(windowId: string): void {
    this.raiseWindow(windowId);
  }

  setWindowState(windowId: string, state: WindowState): void {
    if (this.deferWhileSettling(windowId)) return;
    this.dispatch({ type: "window_state_set", windowId, state });
  }

  toggleMaximized(windowId: string): void {
    if (this.state.modes.isCompact || this.deferWhileSettling(windowId)) return;
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
   *  record, and the record the route answers is taken at once, so the stored path is the reported one
   *  before the broadcast lands (a broadcast from another cause meanwhile must not read the report as a
   *  move to follow). A settling window's first report ends the settling, so it always goes. Answers
   *  whether the shell took the report (or had nothing to take); false when it refused. */
  async reportLocation(windowId: string, path: string, title: string): Promise<boolean> {
    const found = findWindow(this.state, windowId);
    if (found === null) return false;
    if (!found.window.is_settling && found.window.path === path && found.window.title === title) return true;
    this.latestReportedPaths.set(windowId, path);
    let reported: WindowRecord;
    try {
      reported = await this.deps.api.reportWindowLocation(found.desktop.id, windowId, path, title);
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
    this.notifyListeners();
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

  updateWindowResize(delta: PixelPoint): void {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind !== "resize") return;
    this.gesture = { ...gesture, currentRect: resizedRect(gesture.startRect, gesture.edge, delta, this.metrics) };
    this.notifyListeners();
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
    // Already applied (this window's own save, or a broadcast that carried nothing new).
    if (this.state.isLayoutLoaded && layout.updated_at === this.state.layout.updated_at && !isLayoutDirty(this.state))
      return;
    this.dispatch({ type: "layout_loaded", desktopId, layout });
  }

  /** The frame a placement renders at while a gesture moves or resizes it, else its own. */
  gestureRectFor(windowId: string): PixelRect | null {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind === "shortcut" || gesture.windowId !== windowId) return null;
    return gesture.currentRect;
  }

  /** The rectangle the snap preview draws, or null when the drag is in no zone. */
  snapPreviewRect(): PixelRect | null {
    const gesture = this.gesture;
    if (gesture === null || gesture.kind !== "move" || gesture.zone === null) return null;
    return frameToPixels(frameForState(MAXIMIZED_FRAME, gesture.zone), this.backdrop);
  }
}

/** The desktop a fresh window lands on: the deep link's when it exists, else the client's recorded
 *  one when it exists, else the first; null with no desktops. */
export function chooseInitialDesktopId(
  desktops: readonly Desktop[],
  deepLinkDesktopId: string | null,
  recordedDesktopId: string | null,
): string | null {
  if (desktops.length === 0) return null;
  const ids = new Set(desktops.map((desktop) => desktop.id));
  if (deepLinkDesktopId !== null && ids.has(deepLinkDesktopId)) return deepLinkDesktopId;
  if (recordedDesktopId !== null && ids.has(recordedDesktopId)) return recordedDesktopId;
  return desktops[0].id;
}
