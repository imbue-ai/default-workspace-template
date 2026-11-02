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
 * desktop change, ``shell:theme`` after every handshake and on every change of the theme the shell
 * wears, ``shell:shown`` and ``shell:hidden`` as visibility changes, the following rule
 * of plan section 4.6 after every desktops update and every layout load (``shell:navigate`` for a
 * page that declared navigation, a ``src`` reassignment otherwise; an independent window's page follows
 * this client's own stored path, which arrives with the layout), and the pages' own
 * ``shell:capabilities``, ``shell:location``, ``shell:focused``, ``shell:open``, ``shell:start-with-text``,
 * ``shell:message``, and ``shell:open-link``. Messages cross through ``relay.ts``.
 *
 * The phone layout mounts differently (plan-phone-interface.md): under the ``shown`` policy only the shown
 * window's page lives, laid over the whole host, plus the pages it keeps (the pinned chat window's, created
 * whether shown or not, since it is where a phone lands); every other page is destroyed rather than hidden, and a
 * window shown again gets a fresh page.
 *
 * While the overview is up (the multiplayer spec, section 8.2) the active desktop's pages, the minimized ones
 * included, are laid over the overview's tiles instead of the windows: each at the size its window renders at,
 * scaled down to its tile and inert, so the tile shows the page as it is.
 */

import {
  SHELL_CAPABILITIES,
  SHELL_CLOSE_REQUEST,
  SHELL_DRAFT_TEXT,
  SHELL_FOCUSED,
  SHELL_HANDSHAKE,
  SHELL_HIDDEN,
  SHELL_INPUT,
  SHELL_LOCATION,
  SHELL_MESSAGE,
  SHELL_NAVIGATE,
  SHELL_OPEN,
  SHELL_OPEN_LINK,
  SHELL_SHOWN,
  SHELL_KEYBINDINGS_CHANGED,
  SHELL_THEME,
  SHELL_START_WITH_TEXT,
} from "@imbue/workspace-ui/src/app_contract";
import { requestFrameFocus } from "@imbue/workspace-ui/src/terminalFocus";
import { OVERVIEW_PAGE_Z_INDEX, windowPageZIndex } from "../geometry/stacking";
import { windowPageUrl } from "../model/pageUrl";
import { notePageInput } from "../model/Presence";
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
  isAppShownStopped,
} from "../reducers/desktopState";
import type { DesktopState } from "../reducers/desktopState";
import { sendToChildFrame, setChildFrameMessageHandler } from "../relay";
import type { DesktopStore, EmbedderMessage, PageDriver } from "../store/DesktopStore";
import {
  currentTheme,
  isCurrentThemeRemembered,
  onThemeChanged,
  type ThemeRef,
} from "@imbue/workspace-ui/src/themes/themeClient";
import { STANDARD_THEME_ID } from "@imbue/workspace-ui/src/themes/themeBoot";

export const LIVE_PAGE_ATTRIBUTE = "data-live-page";
/** The element of a window's chrome the page is laid over. */
export const WINDOW_CONTENT_ATTRIBUTE = "data-window-content";
/** The element of a window's chrome that carries its id; the gestures bind by it too. */
export const WINDOW_ID_ATTRIBUTE = "data-window-id";
/** The backdrop's root, which carries the active desktop's id: every content box a page lies over (a window's
 *  chrome inside the desktop's stage, an overview tile outside it) is somewhere under it. */
export const DESKTOP_ROOT_ATTRIBUTE = "data-desktop-id";

// App pages are cross-origin iframes (each app owns its own origin), so allow-same-origin only
// lets the framed app be a normal page on ITS origin; it grants nothing on the shell's. An app
// page may open links in real tabs, download files, and raise modals, and lock the pointer for a
// relative-motion program on the Screen, none of which a sandboxed frame may do without these;
// every page gets them, since the shell does not know which apps need which.
export const PAGE_FRAME_SANDBOX =
  "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads " +
  "allow-modals allow-pointer-lock";
// Lets embedded apps (the Screen's viewer) reach the user's clipboard, and ask for Fullscreen-API fullscreen,
// which the Imbue Studio chrome's immersive mode needs delegated down every frame on the way (the Screen
// integration spec, section 5.5).
export const PAGE_FRAME_ALLOW = "clipboard-read; clipboard-write; fullscreen";
// The page's bottom corners follow the chrome's rounding, which a fullscreen window has none of.
const PAGE_CORNERS_CLASS = "rounded-b-(--desk-window-radius)";
// The shell's WindowTitle rule (contracts.md section 1, ``shell/primitives.py``): a longer title is
// refused with a 400, which would leave the whole report, path included, unstored.
export const MAX_WINDOW_TITLE_LENGTH = 256;

/** The `shell:theme` payload (workspace-themes plan section 5.3): a theme the shell wears without remembering it
 *  is a preview, which the page wears without remembering either. */
function themeMessage(
  theme: ThemeRef,
  isRemembered: boolean,
): { readonly theme: string; readonly revision: string; readonly isPreview: boolean } {
  return { theme: theme.id, revision: theme.revision, isPreview: !isRemembered };
}

/** Stops the started layer hearing theme changes: the newest layer takes this seat, as it takes the store's
 *  page-driver seat, so a layer replaced by another (a switch to the phone layout) is let go. */
let stopFollowingTheme: (() => void) | null = null;

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
  /** Whether the page owns the close chord: it gets ``shell:close-request`` and its window stays open. */
  isCloseChordCapable: boolean;
  /** The desktop the page was last introduced to; null before its first load. */
  greetedDesktopId: string | null;
  /** Whether the last handshake said the window was being viewed on somebody else's desktop. */
  greetedIsViewing: boolean;
  lastSentVisibility: boolean | null;
  /** Whether the page is hidden because its app is stopped; it reloads once the app runs again. */
  isHeldForStop: boolean;
}

export interface LivePagesOptions {
  /** The shell document's host and protocol, which every page URL derives from. */
  readonly host: string;
  readonly protocol: string;
}

/** The phone's pages: the shown window's, and the ones it keeps beside it. */
export type ShownMountPolicy = {
  readonly kind: "shown";
  readonly windowId: string | null;
  readonly alsoKeep: readonly string[];
};

/** Which pages live: every page of the desktop's windows, or the phone's shown page and the ones it keeps. */
export type MountPolicy = { readonly kind: "all" } | ShownMountPolicy;

/** The overview's tiles (the multiplayer spec, section 8.2): how far each tile scales its window down. */
export interface OverviewPresentation {
  /** The scale the tile of ``windowId`` draws its page at, or null for a window the overview has no tile for; a
   *  scale that is not positive (a grid no tile fits) hides the page as no tile does. */
  readonly scaleOf: (windowId: string) => number | null;
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
  private mountPolicy: MountPolicy = { kind: "all" };
  /** The overview's tiles while it is up, else null. */
  private overview: OverviewPresentation | null = null;

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
    // Input inside an app's frame never bubbles to the shell's window, so the pages report it (the multiplayer
    // spec, section 4.3) and the heartbeat's activity reads it as this client's own.
    setChildFrameMessageHandler(SHELL_INPUT, () => notePageInput());
    setChildFrameMessageHandler(SHELL_OPEN, (frame, payload) => this.takeOpen(frame, payload));
    setChildFrameMessageHandler(SHELL_START_WITH_TEXT, (frame, payload) => this.takeStartWithText(frame, payload));
    setChildFrameMessageHandler(SHELL_DRAFT_TEXT, (frame, payload) => this.takeDraftText(frame, payload));
    setChildFrameMessageHandler(SHELL_MESSAGE, (frame, payload) => this.takeMessage(frame, payload));
    setChildFrameMessageHandler(SHELL_OPEN_LINK, (frame, payload) => this.takeOpenLink(frame, payload));
    // Focusable, so the shell has somewhere of its own to put the document's focus (``takeFocusFromOtherPages``).
    this.host.tabIndex = -1;
    this.store.setPageDriver(this);
    stopFollowingTheme?.();
    stopFollowingTheme = onThemeChanged((theme, isRemembered) => this.sendTheme(theme, isRemembered));
  }

  /** Set which pages live; applied by the next reconcile. */
  setMountPolicy(policy: MountPolicy): void {
    this.mountPolicy = policy;
  }

  /** Lay the active desktop's pages over the overview's tiles (or back over their windows, for null); applied by
   *  the next reconcile. */
  setOverview(overview: OverviewPresentation | null): void {
    this.overview = overview;
  }

  /** Whether a window's page has been created in this client. */
  hasPage(windowId: string): boolean {
    return this.pages.has(windowId);
  }

  /** Take the document's focus back from any page other than ``windowId``'s (null: from every page) that holds
   *  it. Left in that page, the focus would come back to it with the chrome window's (a tear-out drag returning,
   *  a switch to another application and back), and the page would report it and be raised over the window the
   *  user chose. */
  takeFocusFromOtherPages(windowId: string | null): void {
    const active = document.activeElement;
    if (!(active instanceof HTMLIFrameElement)) return;
    const page = this.pageOfFrame(active);
    if (page === undefined || page.windowId === windowId) return;
    this.host.focus({ preventScroll: true });
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

  resendTheme(appName: string): void {
    for (const page of this.pages.values()) {
      if (page.app === appName && page.greetedDesktopId !== null) {
        sendToChildFrame(
          page.frame,
          SHELL_THEME,
          this.themeMessageFor(page, currentTheme(), isCurrentThemeRemembered()),
        );
      }
    }
  }

  /** The theme a page is sent: the standard one, which puts nothing on the page, for an app whose theming is off. */
  private themeMessageFor(page: LivePage, theme: ThemeRef, isRemembered: boolean): ReturnType<typeof themeMessage> {
    const isThemingOff = appByName(this.store.getState(), page.app)?.theming === "off";
    return themeMessage(isThemingOff ? { id: STANDARD_THEME_ID, revision: "" } : theme, isRemembered);
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

  ownsCloseChord(windowId: string): boolean {
    return this.pages.get(windowId)?.isCloseChordCapable === true;
  }

  /** Put one shown page over its window's content box as it is now, leaving its stacking and
   *  interactivity alone: the per-move step of a drag or resize, which redraws nothing. */
  placePage(windowId: string): void {
    const page = this.pages.get(windowId);
    if (page === undefined || !this.isShown(page)) return;
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
    const policy = this.mountPolicy;
    if (policy.kind === "shown") {
      this.reconcileShown(policy.windowId, policy.alsoKeep, windowsById);
      return;
    }
    if (this.overview !== null) {
      this.reconcileOverview(this.overview, windowsById);
      return;
    }

    const desktop = activeDesktop(state);
    const placements = desktop === null ? [] : activePlacements(state);
    const focused = activeFocusedWindowId(state);
    // A viewed desktop is read-only (the multiplayer spec, section 8.3): every page on it is inert, the way the
    // overview's tiles are, so the owner's windows can be looked at but not typed into.
    const isViewing = this.store.viewedDesktop() !== null;
    const shownIds = new Set<string>();
    placements.forEach((placement, index) => {
      const found = windowsById.get(placement.window_id);
      if (found === undefined || desktop === null) return;
      // A minimized window has no chrome to lie under: its page is hidden until it is restored.
      if (placement.is_minimized) return;
      // A pulled-out window's page is shown in the chrome's own desktop window; one being pulled out right now
      // is already drawn there under the cursor.
      if (placement.is_detached || placement.window_id === this.tornOutWindowId) return;
      const { window } = found;
      const app = appByName(state, window.app);
      if (app === undefined) return;
      const page = this.pages.get(window.id) ?? this.create(window, app);
      shownIds.add(window.id);
      if (!this.prepareShownPage(page, app)) return;
      const box = this.contentBox(window.id);
      if (box === null) {
        this.hide(page);
        return;
      }
      page.wrapper.classList.toggle(PAGE_CORNERS_CLASS, placement.state !== "FULLSCREEN");
      this.show(page, box, index, !this.isGestureActive && !isViewing && window.id === focused);
      // A page is told again when its desktop, or whether that desktop is being viewed, changed since: a terminal
      // attaches to its session as a viewer or as a user by it.
      if (
        page.greetedDesktopId !== null &&
        (page.greetedDesktopId !== desktop.id || page.greetedIsViewing !== isViewing)
      ) {
        this.greet(page);
      }
    });
    for (const page of this.pages.values()) {
      if (!shownIds.has(page.windowId)) this.hide(page);
    }
    this.followIfRevised(windowsById);
    this.focusIfChanged(focused);
  }

  /** The overview (the multiplayer spec, section 8.2): every window of the active desktop the overview has a tile
   *  for, minimized ones included, has its page laid over the tile's content box at the tile's scale, inert; a
   *  pulled-out window's page stays in the chrome's own window, and every other page is hidden. Nothing holds the
   *  frame focus while the overview is up, so the focused window takes it back when the overview goes. */
  private reconcileOverview(
    overview: OverviewPresentation,
    windowsById: ReadonlyMap<string, { window: WindowRecord; desktop: Desktop }>,
  ): void {
    const state = this.store.getState();
    const desktop = activeDesktop(state);
    const placements = desktop === null ? [] : activePlacements(state);
    const shownIds = new Set<string>();
    for (const placement of placements) {
      const found = windowsById.get(placement.window_id);
      const scale = overview.scaleOf(placement.window_id);
      if (found === undefined || desktop === null || scale === null || scale <= 0) continue;
      if (placement.is_detached || placement.window_id === this.tornOutWindowId) continue;
      const app = appByName(state, found.window.app);
      if (app === undefined) continue;
      const page = this.pages.get(found.window.id) ?? this.create(found.window, app);
      shownIds.add(found.window.id);
      if (!this.prepareShownPage(page, app)) continue;
      const box = this.contentBox(found.window.id);
      if (box === null) {
        this.hide(page);
        continue;
      }
      this.showScaled(page, box, scale);
      if (page.greetedDesktopId !== null && page.greetedDesktopId !== desktop.id) this.greet(page);
    }
    for (const page of this.pages.values()) {
      if (!shownIds.has(page.windowId)) this.hide(page);
    }
    this.followIfRevised(windowsById);
    this.focusIfChanged(null);
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
    if (!this.prepareShownPage(page, app)) return;
    const hostBox = this.host.getBoundingClientRect();
    this.show(page, { left: 0, top: 0, width: hostBox.width, height: hostBox.height }, 0, true);
    if (page.greetedDesktopId !== null && page.greetedDesktopId !== found.desktop.id) this.greet(page);
    this.followIfRevised(windowsById);
    this.focusIfChanged(soloWindowId);
  }

  /** The phone's policy: the shown window's page over the whole host, live and focused; the kept ones mounted and
   *  hidden; every other page destroyed. */
  private reconcileShown(
    shownId: string | null,
    alsoKeep: readonly string[],
    windowsById: ReadonlyMap<string, { window: WindowRecord; desktop: Desktop }>,
  ): void {
    const state = this.store.getState();
    const mounted = new Set([...alsoKeep, ...(shownId === null ? [] : [shownId])]);
    for (const page of [...this.pages.values()]) {
      if (!mounted.has(page.windowId)) this.destroy(page);
    }
    for (const windowId of mounted) {
      const found = windowsById.get(windowId);
      const app = found === undefined ? undefined : appByName(state, found.window.app);
      if (found === undefined || app === undefined) continue;
      const page = this.pages.get(windowId) ?? this.create(found.window, app);
      if (windowId !== shownId || !this.prepareShownPage(page, app)) {
        this.hide(page);
        continue;
      }
      const hostBox = this.host.getBoundingClientRect();
      this.show(page, { left: 0, top: 0, width: hostBox.width, height: hostBox.height }, 0, true);
      if (page.greetedDesktopId !== null && page.greetedDesktopId !== found.desktop.id) this.greet(page);
    }
    this.followIfRevised(windowsById);
    this.focusIfChanged(shownId !== null && this.pages.has(shownId) ? shownId : null);
  }

  /** Whether a page about to be shown can be: the page of an app that is stopped and that nothing brings back on
   *  a request is hidden and held (false), and a held page is reloaded once its app runs again. A stoppable app's
   *  page stays: the shell's parker answers it with a loading page until the app is up. */
  private prepareShownPage(page: LivePage, app: AppRecord): boolean {
    if (isAppShownStopped(this.store.getState(), app)) {
      page.isHeldForStop = true;
      this.hide(page);
      return false;
    }
    if (page.isHeldForStop) {
      page.isHeldForStop = false;
      this.reloadPage(page);
    }
    return true;
  }

  /** Follow the windows' paths only after the shell's own desktops update or layout load, never on a redraw or
   *  a local edit (an open, a close, a settings answer): between a page's own location report and the broadcast
   *  that stores it, the stored path is still the old one, and nothing must send the page back there. */
  private followIfRevised(windowsById: ReadonlyMap<string, { window: WindowRecord; desktop: Desktop }>): void {
    const desktopsRevision = this.store.getDesktopsRevision();
    const layoutLoadsRevision = this.store.getLayoutLoadsRevision();
    if (
      desktopsRevision === this.followedDesktopsRevision &&
      layoutLoadsRevision === this.followedLayoutLoadsRevision
    ) {
      return;
    }
    this.followedDesktopsRevision = desktopsRevision;
    this.followedLayoutLoadsRevision = layoutLoadsRevision;
    this.follow(windowsById);
  }

  /** When the focused window changes, however it was raised, take the document's focus off every other window's
   *  page and give the focused window's page the frame focus once. */
  private focusIfChanged(focused: string | null): void {
    if (focused === this.lastFocusedWindowId) return;
    this.lastFocusedWindowId = focused;
    this.takeFocusFromOtherPages(focused);
    const page = focused === null ? undefined : this.pages.get(focused);
    if (page !== undefined) requestFrameFocus(page.wrapper);
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

  /** A window's content box in the host's own pixels. The measurement is in screen pixels: while a viewed desktop is
   *  drawn in a letterbox (the multiplayer spec, section 8.3), the host and the chrome lie under one scale, and a
   *  page's position and size are in the host's unscaled pixels, so the box is divided by that scale. */
  private contentBox(windowId: string): HostRect | null {
    // Searched from the backdrop's root, not the host's parent: the overview's tiles lie outside the desktop's
    // stage the host sits in.
    const root = this.host.closest<HTMLElement>(`[${DESKTOP_ROOT_ATTRIBUTE}]`) ?? this.host.parentElement;
    const content = root?.querySelector<HTMLElement>(
      `[${WINDOW_ID_ATTRIBUTE}="${CSS.escape(windowId)}"] [${WINDOW_CONTENT_ATTRIBUTE}]`,
    );
    if (content === undefined || content === null) return null;
    const box = content.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) return null;
    const origin = this.host.getBoundingClientRect();
    const scale = this.store.viewingLetterbox()?.scale ?? 1;
    return {
      left: (box.left - origin.left) / scale,
      top: (box.top - origin.top) / scale,
      width: box.width / scale,
      height: box.height / scale,
    };
  }

  private create(window: WindowRecord, app: AppRecord): LivePage {
    const wrapper = document.createElement("div");
    // A surface while the page loads, under the chrome (whose content box is transparent) and over the
    // wallpaper; the bottom corners follow the chrome's rounding.
    wrapper.className = `live-page absolute overflow-hidden ${PAGE_CORNERS_CLASS} bg-page`;
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
      isCloseChordCapable: false,
      greetedDesktopId: null,
      greetedIsViewing: false,
      lastSentVisibility: null,
      isHeldForStop: false,
    };
    // Every load, not just the first: a reload (a Refresh, or the page's own) is a fresh page that
    // has to be told who it is again, and has declared nothing yet.
    frame.addEventListener("load", () => {
      page.isNavigationCapable = false;
      page.isCloseChordCapable = false;
      page.lastSentVisibility = null;
      this.greet(page);
      this.syncVisibility(page, this.isShown(page));
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
    style.transform = "";
  }

  /** Lay a page over an overview tile's box: at the size its window renders at (the box over the scale), scaled
   *  down from the box's top-left corner to fill it, inert, above the overview's scrim. */
  private showScaled(page: LivePage, box: HostRect, scale: number): void {
    const style = page.wrapper.style;
    style.left = `${box.left}px`;
    style.top = `${box.top}px`;
    style.width = `${box.width / scale}px`;
    style.height = `${box.height / scale}px`;
    style.transformOrigin = "top left";
    style.transform = `scale(${scale})`;
    style.zIndex = OVERVIEW_PAGE_Z_INDEX;
    style.pointerEvents = "none";
    style.display = "";
    this.syncVisibility(page, true);
  }

  private show(page: LivePage, box: HostRect, stackIndex: number, isInteractive: boolean): void {
    this.position(page, box);
    const style = page.wrapper.style;
    style.zIndex = windowPageZIndex(stackIndex);
    style.pointerEvents = isInteractive ? "auto" : "none";
    style.display = "";
    this.syncVisibility(page, true);
  }

  /** Out of sight, and out of the document's focus: a hidden frame left holding it is the one the browser
   *  restores focus to when the chrome window's focus comes back, and the page would then report focus it
   *  never got from the user. */
  private hide(page: LivePage): void {
    page.wrapper.style.display = "none";
    if (document.activeElement === page.frame) this.host.focus({ preventScroll: true });
    this.syncVisibility(page, false);
  }

  private isShown(page: LivePage): boolean {
    return page.wrapper.style.display !== "none";
  }

  /** The handshake names the desktop the page's window is on (a hidden page can reload while another desktop
   *  is active), falling back to the active one only for a window gone from every desktop. */
  private greet(page: LivePage): void {
    const state = this.store.getState();
    const found = findWindow(state, page.windowId);
    const desktopId = found?.desktop.id ?? state.activeDesktopId ?? "";
    const path = found === null ? "" : this.pathSeen(page, found, state);
    // Viewed means the window's desktop is the active one and not the viewer's own (the multiplayer spec, 8.3).
    const isViewing = desktopId === state.activeDesktopId && this.store.viewedDesktop() !== null;
    sendToChildFrame(page.frame, SHELL_HANDSHAKE, {
      clientId: state.clientId,
      windowId: page.windowId,
      desktopId,
      app: page.app,
      path,
      opensLocalFiles: this.store.getOpensLocalFiles(),
      isViewing,
    });
    page.greetedDesktopId = desktopId;
    page.greetedIsViewing = isViewing;
    sendToChildFrame(page.frame, SHELL_THEME, this.themeMessageFor(page, currentTheme(), isCurrentThemeRemembered()));
  }

  /** Tell every greeted page the theme the desktop now wears. */
  private sendTheme(theme: ThemeRef, isRemembered: boolean): void {
    for (const page of this.pages.values()) {
      if (page.greetedDesktopId !== null) {
        sendToChildFrame(page.frame, SHELL_THEME, this.themeMessageFor(page, theme, isRemembered));
      }
    }
  }

  /** Tell every greeted page somebody changed a keybinding; each re-fetches its own resolved chords. */
  broadcastKeybindingsChanged(): void {
    for (const page of this.pages.values()) {
      if (page.greetedDesktopId !== null) {
        sendToChildFrame(page.frame, SHELL_KEYBINDINGS_CHANGED, {});
      }
    }
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

  /** ``shell:capabilities`` from a page: what it can do, and the handshake again. The contract module is up
   *  before the frame's load (which waits for the page's own frames, a terminal's pty among them), so greeting it
   *  here tells a page whether it is viewed as soon as it can listen, and a viewed terminal attaches as a viewer
   *  from its first attach rather than after its load; the page takes a repeated handshake as it is. */
  private takeCapabilities(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
    const page = this.pageOfFrame(frame);
    if (page === undefined) return;
    page.isNavigationCapable = payload.navigation === true;
    page.isCloseChordCapable = payload.closeChord === true;
    this.greet(page);
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

  /** ``shell:focused`` from a page: its window is raised. A page that is not shown (minimized, pulled out into the
   *  chrome's own window, or being pulled out) cannot have been chosen by the user, and a raise would bring its
   *  window back onto the desktop. A viewed page's focus raises nothing either (the multiplayer spec, section
   *  8.3): the stage is inert, so the focus is the page's own doing (a composer focused on load), nobody's
   *  gesture, and nothing to tell the user about. */
  private takeFocused(frame: HTMLIFrameElement): void {
    const page = this.pageOfFrame(frame);
    if (page === undefined || !this.isShown(page) || this.store.viewedDesktop() !== null) return;
    this.store.raiseWindow(page.windowId);
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

  /** ``shell:message {message}`` from a page: a message of the page's own (a ``type`` and its fields, such as
   *  ``open:file``), delivered to the apps registered for its type, with the page's app as its sender. The frame has
   *  to be one the shell created. */
  private takeMessage(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
    const page = this.pageOfFrame(frame);
    if (page === undefined) return;
    const message = payload.message;
    if (message === null || typeof message !== "object" || typeof (message as { type?: unknown }).type !== "string") {
      console.warn(`[si] shell:message ignored: it carried no message with a type (${JSON.stringify(payload)})`);
      return;
    }
    void this.store.relayEmbedderMessage(message as EmbedderMessage, page.app);
  }

  /** ``shell:open-link {url}`` from a page: a link that is not the page's own (a local URL, a ``file:`` URL, another
   *  app's address or another workspace's, an external web, ``mailto:`` or ``tel:`` link), which the shell opens where
   *  ``openLink`` says, or refuses with a notice. The frame has to be one the shell created. */
  private takeOpenLink(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
    const page = this.pageOfFrame(frame);
    if (page === undefined) return;
    const url = payload.url;
    if (typeof url !== "string" || url === "") {
      console.warn(`[si] shell:open-link ignored: it carried no url (${JSON.stringify(payload)})`);
      return;
    }
    void this.store.openLink(url, window.location.host, page.app);
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
