/**
 * The live pages (desktop-interface plan section 6.4): one iframe per window per client, keyed
 * by window id, created when the window is first shown in this client and destroyed only when
 * the window closes or its desktop is deleted. A page is never re-parented (that reloads it):
 * hidden pages are ``display: none``, and the reconcile step positions each page over its
 * window's content box (``placePage`` re-places one page per pointer move of a drag or resize,
 * with no redraw), in the same stacking context as the window chrome so a window's edges and
 * shield stay clickable over a cross-origin page. Every page but the focused one is inert
 * (``pointer-events: none``), and every page is inert for the length of a press on a handle,
 * which is longer than the drag it may become: the pixels a press spends reaching the drag
 * threshold have to be ones the shell can see.
 *
 * The shell side of the app contract lives here too: the handshake after every load and on a
 * desktop change, ``shell:shown`` and ``shell:hidden`` as visibility changes, the following rule
 * of plan section 4.6 after every desktops update and every layout load (``shell:navigate`` for a
 * page that declared navigation, a ``src`` reassignment otherwise; an independent window's page follows
 * this client's own stored path, which arrives with the layout), and the pages' own
 * ``shell:capabilities``, ``shell:location``, ``shell:focused``, ``shell:open``, and
 * ``shell:start-with-text``. Messages cross through ``relay.ts``.
 */

import {
  SHELL_CAPABILITIES,
  SHELL_CLOSE_REQUEST,
  SHELL_DRAFT_TEXT,
  SHELL_FOCUSED,
  SHELL_HANDSHAKE,
  SHELL_HIDDEN,
  SHELL_LOCATION,
  SHELL_NAVIGATE,
  SHELL_OPEN,
  SHELL_SHOWN,
  SHELL_START_WITH_TEXT,
} from "@imbue/workspace-ui/src/app_contract";
import { requestFrameFocus } from "@imbue/workspace-ui/src/terminalFocus";
import { windowPageUrl } from "../model/pageUrl";
import type { AppRecord, Desktop, WindowRecord } from "../model/records";
import { navigationsToFollow } from "../reducers/following";
import type { PageReport } from "../reducers/following";
import {
  activeDesktop,
  activeFocusedWindowId,
  activePlacements,
  appByName,
  effectiveWindow,
  effectiveWindowTitle,
  findWindow,
} from "../reducers/desktopState";
import type { DesktopState } from "../reducers/desktopState";
import { sendToChildFrame, setChildFrameMessageHandler } from "../relay";
import type { DesktopStore, PageDriver } from "../store/DesktopStore";

export const LIVE_PAGE_ATTRIBUTE = "data-live-page";
/** The element of a window's chrome the page is laid over. */
export const WINDOW_CONTENT_ATTRIBUTE = "data-window-content";
/** The element of a window's chrome that carries its id; the gestures bind by it too. */
export const WINDOW_ID_ATTRIBUTE = "data-window-id";

// App pages are cross-origin iframes (each app owns its own origin), so allow-same-origin only
// lets the framed app be a normal page on ITS origin; it grants nothing on the shell's. An app
// page may open links in real tabs, download files, and raise modals, none of which a sandboxed
// frame may do without these; every page gets them, since the shell does not know which apps
// need which.
export const PAGE_FRAME_SANDBOX =
  "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals";
// Lets embedded apps (the browser fleet viewer) reach the user's clipboard.
export const PAGE_FRAME_ALLOW = "clipboard-read; clipboard-write";
// The shell's WindowTitle rule (contracts.md section 1, ``shell/primitives.py``): a longer title is
// refused with a 400, which would leave the whole report, path included, unstored.
export const MAX_WINDOW_TITLE_LENGTH = 256;

interface LivePage {
  readonly windowId: string;
  readonly app: string;
  readonly wrapper: HTMLElement;
  readonly frame: HTMLIFrameElement;
  /** The path the page last reported, or was last pointed at. */
  lastReportedPath: string;
  /** A report of the page's own the shell has not yet shown in a desktops update: the path the page left
   *  (``fromPath``, a stored record still naming it is stale) and the path it reported. */
  pendingReport: { readonly fromPath: string; readonly path: string } | null;
  isNavigationCapable: boolean;
  /** The desktop the page was last introduced to; null before its first load. */
  greetedDesktopId: string | null;
  lastSentVisibility: boolean | null;
  /** Whether the page is hidden because its app is stopped; it reloads once the app runs again. */
  isHeldForStop: boolean;
}

export interface LivePagesOptions {
  /** The shell document's host and protocol, which every page URL derives from. */
  readonly host: string;
  readonly protocol: string;
}

/** A pixel box relative to the pages' host. */
interface HostRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

export class LivePagesLayer implements PageDriver {
  private readonly pages = new Map<string, LivePage>();
  private isGestureActive = false;
  /** The window a tear-out drag has pulled past the viewport: its page is hidden until the drag comes back
   *  inside or ends (the pull-out-window spec). */
  private tornOutWindowId: string | null = null;
  private lastFocusedWindowId: string | null = null;
  /** The shell's desktops revision the pages last followed: a linked window's stored path changes only with it. */
  private followedDesktopsRevision = 0;
  /** The shell's layout revision the pages last followed: an independent window's stored path arrives with it. */
  private followedLayoutLoadsRevision = 0;

  constructor(
    private readonly host: HTMLElement,
    private readonly store: DesktopStore,
    private readonly options: LivePagesOptions,
  ) {}

  /** Register the contract handlers and take the store's page-driver seat. */
  start(): void {
    setChildFrameMessageHandler(SHELL_CAPABILITIES, (frame, payload) => this.takeCapabilities(frame, payload));
    setChildFrameMessageHandler(SHELL_LOCATION, (frame, payload) => this.takeLocation(frame, payload));
    setChildFrameMessageHandler(SHELL_FOCUSED, (frame) => this.takeFocused(frame));
    setChildFrameMessageHandler(SHELL_OPEN, (frame, payload) => this.takeOpen(frame, payload));
    setChildFrameMessageHandler(SHELL_START_WITH_TEXT, (frame, payload) => this.takeStartWithText(frame, payload));
    setChildFrameMessageHandler(SHELL_DRAFT_TEXT, (frame, payload) => this.takeDraftText(frame, payload));
    this.store.setPageDriver(this);
  }

  /** Whether a window's page has been created in this client. */
  hasPage(windowId: string): boolean {
    return this.pages.has(windowId);
  }

  /** Make every page inert for the length of a press on a handle (the drag it may become begins
   *  partway through), and give the focused one its pointer back after. */
  setGestureActive(isActive: boolean): void {
    if (this.isGestureActive === isActive) return;
    this.isGestureActive = isActive;
    this.reconcile();
  }

  /** Hide the page of the window a tear-out drag is pulling out (the chrome shows it elsewhere), or show it
   *  again: the per-move step of that drag, which redraws nothing. */
  setTornOutWindow(windowId: string | null): void {
    if (this.tornOutWindowId === windowId) return;
    this.tornOutWindowId = windowId;
    this.reconcile();
  }

  reload(windowId: string): void {
    const page = this.pages.get(windowId);
    if (page !== undefined) this.reloadPage(page);
  }

  reloadApp(appName: string): void {
    for (const page of this.pages.values()) {
      if (page.app === appName) this.reloadPage(page);
    }
  }

  /** Reload a page at its window's stored path (the page may have moved since it was first pointed). */
  private reloadPage(page: LivePage): void {
    const state = this.store.getState();
    const found = findWindow(state, page.windowId);
    const app = found === null ? undefined : appByName(state, found.window.app);
    if (found === null || app === undefined) return;
    this.pointPageAt(page, app, this.pathSeen(page, found, state));
  }

  /** Whether the layout holds this client's own path for the window: an independent window's arrives with its
   *  desktop's layout, so it is known only while that desktop is the active one and the layout has loaded. */
  private isOwnPathKnown(found: { window: WindowRecord; desktop: Desktop }, state: DesktopState): boolean {
    return found.desktop.id === state.activeDesktopId && state.isLayoutLoaded;
  }

  /** The path this client's page of the window is at: a linked window's shared record, an independent window's
   *  own stored path when it is known, else where the layer last put or saw the page. */
  private pathSeen(page: LivePage, found: { window: WindowRecord; desktop: Desktop }, state: DesktopState): string {
    if (found.window.scope === "independent" && !this.isOwnPathKnown(found, state)) return page.lastReportedPath;
    return effectiveWindow(state, found.window).path;
  }

  /** Point the frame at the app's page for ``path`` (cross-origin, so a reload is a ``src`` reassignment). */
  private pointPageAt(page: LivePage, app: AppRecord, path: string): void {
    page.lastReportedPath = path;
    page.frame.setAttribute("src", windowPageUrl(app, path, this.options.host, this.options.protocol));
  }

  requestClose(windowId: string): void {
    const page = this.pages.get(windowId);
    if (page !== undefined) sendToChildFrame(page.frame, SHELL_CLOSE_REQUEST);
  }

  /** Put one shown page over its window's content box as it is now, leaving its stacking and
   *  interactivity alone: the per-move step of a drag or resize, which redraws nothing. */
  placePage(windowId: string): void {
    const page = this.pages.get(windowId);
    if (page === undefined || page.wrapper.style.display === "none") return;
    const box = this.contentBox(windowId);
    if (box !== null) this.position(page, box);
  }

  /**
   * Put every page where its window is. Run after each redraw, once the window chrome is in the
   * DOM to measure: pages of the active desktop's shown windows are created and positioned; the
   * rest are hidden; a page whose window is gone from every desktop is destroyed; and every page
   * follows its window's path.
   */
  reconcile(): void {
    const state = this.store.getState();
    const windowsById = new Map<string, { window: WindowRecord; desktop: Desktop }>();
    for (const desktop of state.desktops) {
      for (const window of desktop.windows) windowsById.set(window.id, { window, desktop });
    }
    for (const [windowId, page] of [...this.pages]) {
      if (!windowsById.has(windowId)) this.destroy(page);
    }

    const soloWindowId = this.store.getSoloWindowId();
    if (soloWindowId !== null) {
      this.reconcileSolo(soloWindowId, windowsById);
      return;
    }

    const desktop = activeDesktop(state);
    const placements = desktop === null ? [] : activePlacements(state);
    const focused = activeFocusedWindowId(state);
    const shownIds = new Set<string>();
    placements.forEach((placement, index) => {
      const found = windowsById.get(placement.window_id);
      if (found === undefined || desktop === null || placement.is_minimized) return;
      // A pulled-out window's page is shown in the chrome's own desktop window; one being pulled out right now
      // is already drawn there under the cursor.
      if (placement.is_detached || placement.window_id === this.tornOutWindowId) return;
      const { window } = found;
      const app = appByName(state, window.app);
      if (app === undefined) return;
      const page = this.pages.get(window.id) ?? this.create(window, app);
      shownIds.add(window.id);
      if (!app.is_running) {
        page.isHeldForStop = true;
        this.hide(page);
        return;
      }
      if (page.isHeldForStop) {
        page.isHeldForStop = false;
        this.reloadPage(page);
      }
      const box = this.contentBox(window.id);
      if (box === null) {
        this.hide(page);
        return;
      }
      this.show(page, box, index, !this.isGestureActive && window.id === focused);
      if (page.greetedDesktopId !== null && page.greetedDesktopId !== desktop.id) this.greet(page);
    });
    for (const page of this.pages.values()) {
      if (!shownIds.has(page.windowId)) this.hide(page);
    }

    // Only after the shell's own desktops update or layout load, never on a redraw or a local edit (an open, a
    // close, a settings answer): between a page's own location report and the broadcast that stores it, the
    // stored path is still the old one, and nothing must send the page back there.
    const desktopsRevision = this.store.getDesktopsRevision();
    const layoutLoadsRevision = this.store.getLayoutLoadsRevision();
    if (
      desktopsRevision !== this.followedDesktopsRevision ||
      layoutLoadsRevision !== this.followedLayoutLoadsRevision
    ) {
      this.followedDesktopsRevision = desktopsRevision;
      this.followedLayoutLoadsRevision = layoutLoadsRevision;
      this.follow(windowsById);
    }

    if (focused !== this.lastFocusedWindowId) {
      this.lastFocusedWindowId = focused;
      const page = focused === null ? undefined : this.pages.get(focused);
      if (page !== undefined) requestFrameFocus(page.wrapper);
    }
  }

  /** Solo mode (the pull-out-window spec, section 7.5): the one window's page over the whole host, live, and no
   *  other page at all. It follows its window's path like any page. */
  private reconcileSolo(
    soloWindowId: string,
    windowsById: ReadonlyMap<string, { window: WindowRecord; desktop: Desktop }>,
  ): void {
    const state = this.store.getState();
    const found = windowsById.get(soloWindowId);
    const app = found === undefined ? undefined : appByName(state, found.window.app);
    for (const page of this.pages.values()) {
      if (page.windowId !== soloWindowId) this.hide(page);
    }
    if (found === undefined || app === undefined) return;
    const page = this.pages.get(soloWindowId) ?? this.create(found.window, app);
    if (!app.is_running) {
      page.isHeldForStop = true;
      this.hide(page);
      return;
    }
    if (page.isHeldForStop) {
      page.isHeldForStop = false;
      this.reloadPage(page);
    }
    const hostBox = this.host.getBoundingClientRect();
    this.show(page, { left: 0, top: 0, width: hostBox.width, height: hostBox.height }, 0, true);
    if (page.greetedDesktopId !== null && page.greetedDesktopId !== found.desktop.id) this.greet(page);
    const desktopsRevision = this.store.getDesktopsRevision();
    const layoutLoadsRevision = this.store.getLayoutLoadsRevision();
    if (
      desktopsRevision !== this.followedDesktopsRevision ||
      layoutLoadsRevision !== this.followedLayoutLoadsRevision
    ) {
      this.followedDesktopsRevision = desktopsRevision;
      this.followedLayoutLoadsRevision = layoutLoadsRevision;
      this.follow(windowsById);
    }
    if (this.lastFocusedWindowId !== soloWindowId) {
      this.lastFocusedWindowId = soloWindowId;
      requestFrameFocus(page.wrapper);
    }
  }

  private follow(windowsById: ReadonlyMap<string, { window: WindowRecord; desktop: Desktop }>): void {
    const state = this.store.getState();
    // A navigation this client asked for itself is meant even when it points the page back at a path the page
    // just reported leaving (the same draft handed over twice): the guard below is for stale snapshots, not this.
    const own = this.store.takeOwnNavigation();
    if (own !== null) {
      const page = this.pages.get(own.windowId);
      if (page !== undefined) page.pendingReport = null;
    }
    const reports = new Map<string, PageReport>();
    const windows: WindowRecord[] = [];
    for (const page of this.pages.values()) {
      const found = windowsById.get(page.windowId);
      if (found === undefined) continue;
      // A hidden page of an independent window on another desktop stays where it is until that desktop is
      // shown again, and nothing moves one while the active desktop's layout is still being read.
      if (found.window.scope === "independent" && !this.isOwnPathKnown(found, state)) continue;
      // The window as this client sees it: an independent window at this client's own stored path.
      const seen = effectiveWindow(state, found.window);
      // A stored record still naming the path a page reported leaving is a snapshot from before the shell
      // took the report (a broadcast from another cause meanwhile): it must not send the page back.
      if (page.pendingReport !== null) {
        if (seen.path === page.pendingReport.fromPath) continue;
        page.pendingReport = null;
      }
      windows.push(seen);
      reports.set(page.windowId, {
        lastReportedPath: page.lastReportedPath,
        isNavigationCapable: page.isNavigationCapable,
      });
    }
    for (const action of navigationsToFollow(windows, reports)) {
      const page = this.pages.get(action.windowId);
      const found = windowsById.get(action.windowId);
      if (page === undefined || found === undefined) continue;
      // Recorded at once, so a second broadcast before the page lands does not move it twice.
      page.lastReportedPath = action.path;
      if (action.mode === "navigate") {
        sendToChildFrame(page.frame, SHELL_NAVIGATE, { path: action.path });
      } else {
        const app = appByName(this.store.getState(), found.window.app);
        if (app !== undefined) this.pointPageAt(page, app, action.path);
      }
    }
  }

  private contentBox(windowId: string): HostRect | null {
    const content = this.host.parentElement?.querySelector<HTMLElement>(
      `[${WINDOW_ID_ATTRIBUTE}="${CSS.escape(windowId)}"] [${WINDOW_CONTENT_ATTRIBUTE}]`,
    );
    if (content === undefined || content === null) return null;
    const box = content.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) return null;
    const origin = this.host.getBoundingClientRect();
    return { left: box.left - origin.left, top: box.top - origin.top, width: box.width, height: box.height };
  }

  private create(window: WindowRecord, app: AppRecord): LivePage {
    const wrapper = document.createElement("div");
    // A surface while the page loads, under the chrome (whose content box is transparent) and over the
    // wallpaper; the bottom corners follow the chrome's rounding.
    wrapper.className = "live-page absolute overflow-hidden rounded-b-(--desk-window-radius) bg-page";
    wrapper.style.display = "none";
    const frame = document.createElement("iframe");
    frame.setAttribute(LIVE_PAGE_ATTRIBUTE, window.id);
    frame.setAttribute("sandbox", PAGE_FRAME_SANDBOX);
    frame.setAttribute("allow", PAGE_FRAME_ALLOW);
    const state = this.store.getState();
    frame.title = effectiveWindowTitle(state, window, app);
    frame.className = "block h-full w-full border-0";
    wrapper.appendChild(frame);
    const openingPath = effectiveWindow(state, window).path;
    const page: LivePage = {
      windowId: window.id,
      app: window.app,
      wrapper,
      frame,
      lastReportedPath: openingPath,
      pendingReport: null,
      isNavigationCapable: false,
      greetedDesktopId: null,
      lastSentVisibility: null,
      isHeldForStop: false,
    };
    // Every load, not just the first: a reload (a Refresh, or the page's own) is a fresh page that
    // has to be told who it is again, and has declared nothing yet.
    frame.addEventListener("load", () => {
      page.isNavigationCapable = false;
      page.lastSentVisibility = null;
      this.greet(page);
      this.syncVisibility(page, page.wrapper.style.display !== "none");
    });
    this.pages.set(window.id, page);
    this.host.appendChild(wrapper);
    this.pointPageAt(page, app, openingPath);
    return page;
  }

  private destroy(page: LivePage): void {
    this.pages.delete(page.windowId);
    page.wrapper.remove();
  }

  private position(page: LivePage, box: HostRect): void {
    const style = page.wrapper.style;
    style.left = `${box.left}px`;
    style.top = `${box.top}px`;
    style.width = `${box.width}px`;
    style.height = `${box.height}px`;
  }

  private show(page: LivePage, box: HostRect, stackIndex: number, isInteractive: boolean): void {
    this.position(page, box);
    const style = page.wrapper.style;
    // Interleaved with the window chrome: chrome at 2i+2 sits over its own page at 2i+1 and over
    // every lower window's page and chrome.
    style.zIndex = String(2 * stackIndex + 1);
    style.pointerEvents = isInteractive ? "auto" : "none";
    style.display = "";
    this.syncVisibility(page, true);
  }

  private hide(page: LivePage): void {
    page.wrapper.style.display = "none";
    this.syncVisibility(page, false);
  }

  /** The handshake names the desktop the page's window is on (a hidden page can reload while another desktop
   *  is active), falling back to the active one only for a window gone from every desktop. */
  private greet(page: LivePage): void {
    const state = this.store.getState();
    const found = findWindow(state, page.windowId);
    const desktopId = found?.desktop.id ?? state.activeDesktopId ?? "";
    const path = found === null ? "" : this.pathSeen(page, found, state);
    sendToChildFrame(page.frame, SHELL_HANDSHAKE, {
      clientId: state.clientId,
      windowId: page.windowId,
      desktopId,
      app: page.app,
      path,
    });
    page.greetedDesktopId = desktopId;
  }

  private syncVisibility(page: LivePage, isVisible: boolean): void {
    // Nothing until the page has had its handshake: before its load there is nobody listening.
    if (page.greetedDesktopId === null || page.lastSentVisibility === isVisible) return;
    page.lastSentVisibility = isVisible;
    sendToChildFrame(page.frame, isVisible ? SHELL_SHOWN : SHELL_HIDDEN);
  }

  private pageOfFrame(frame: HTMLIFrameElement): LivePage | undefined {
    const windowId = frame.getAttribute(LIVE_PAGE_ATTRIBUTE);
    return windowId === null ? undefined : this.pages.get(windowId);
  }

  private takeCapabilities(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
    const page = this.pageOfFrame(frame);
    if (page !== undefined) page.isNavigationCapable = payload.navigation === true;
  }

  private takeLocation(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
    const page = this.pageOfFrame(frame);
    const path = payload.path;
    if (page === undefined || typeof path !== "string" || path === "") return;
    const title = typeof payload.title === "string" ? payload.title.trim().slice(0, MAX_WINDOW_TITLE_LENGTH) : "";
    // Remembered before the post, so this client's own report never navigates the page.
    page.lastReportedPath = path;
    const state = this.store.getState();
    const stored = findWindow(state, page.windowId);
    if (stored !== null) {
      const seenPath = effectiveWindow(state, stored.window).path;
      if (seenPath !== path) page.pendingReport = { fromPath: seenPath, path };
    }
    if (title !== "") page.frame.title = title;
    void this.store.reportLocation(page.windowId, path, title).then((isTaken) => {
      // A report the shell refused leaves the stored path in force, and the page follows it again.
      if (!isTaken && page.pendingReport?.path === path) page.pendingReport = null;
    });
  }

  private takeFocused(frame: HTMLIFrameElement): void {
    const page = this.pageOfFrame(frame);
    if (page !== undefined) this.store.raiseWindow(page.windowId);
  }

  /** ``shell:start-with-text {text}`` from a page: the launcher's primary text action runs with it, on the active
   *  desktop (a page can only be pressed there); the frame has to be one the shell created. */
  private takeStartWithText(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
    const text = this.textFromPage(frame, payload, SHELL_START_WITH_TEXT);
    if (text !== null) void this.store.startWithText(text);
  }

  /** ``shell:draft-text {text}`` from a page (element-reference-menu plan section 5): the text is drafted into the
   *  chat the pinned draft launch path names; the frame has to be one the shell created. */
  private takeDraftText(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
    const text = this.textFromPage(frame, payload, SHELL_DRAFT_TEXT);
    if (text !== null) void this.store.draftText(text);
  }

  /** The text a page's text-carrying message holds: null when the frame is not one the shell created, or when
   *  the payload carries no string text (warned, with the message's type). */
  private textFromPage(frame: HTMLIFrameElement, payload: Record<string, unknown>, type: string): string | null {
    if (this.pageOfFrame(frame) === undefined) return null;
    const text = payload.text;
    if (typeof text !== "string") {
      console.warn(`[si] ${type} ignored: it carried no text (${JSON.stringify(payload)})`);
      return null;
    }
    return text;
  }

  private takeOpen(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
    const page = this.pageOfFrame(frame);
    if (page === undefined) return;
    const path = payload.path;
    if (typeof path !== "string") {
      console.warn(`[si] shell:open ignored: it carried no path (${JSON.stringify(payload)})`);
      return;
    }
    const ifPresent = payload.ifPresent === "new" ? "new" : "focus";
    void this.store.openPathFromWindow(page.windowId, path, ifPresent);
  }
}
