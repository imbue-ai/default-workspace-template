/**
 * The live pages, and which pane is showing each one.
 *
 * There is ONE live page per object, machine-wide: an app instance open in
 * three projects is one iframe and one document, not three. A project is a
 * *view* that may or may not include the object, and a pane is only a place
 * to show it at some size. An app's objects are its numbered instances (see
 * the ``?instance=`` half of the key grammar below), so two panes of one app
 * are two instances with a page each -- one page cannot show in two panes at
 * once. Removing an iframe from the document destroys it, and
 * re-parenting one reloads it, so the element holding a page must never leave
 * the DOM -- not on a view switch, not on a tab close, not on a re-arrange.
 * Destroying the object behind it is the one thing that takes it out.
 *
 * So this registry is global and keyed by the object's identity (its *live
 * key*), and a dockview panel is demoted to a **slot**: an empty div that
 * dockview creates, positions, hides and disposes at will. A surface lives in
 * the same layer dockview positions its own panel overlays in, mirrors the
 * geometry and visibility of whichever slot currently shows it, and outlives
 * every one of them.
 *
 * DockviewWorkspace owns everything that knows what a page *is* -- which
 * component renders it, what it is called, what filing it means. This module
 * owns only where the live elements are, how big, and whether anything is
 * looking at them.
 */

import m from "mithril";
import type { DockviewPanelApi } from "dockview-core";

// The per-workspace browser fleet's service name. Each browser is addressed
// per session rather than per service, so it is the one service whose live key
// carries a query.
export const BROWSER_SERVICE_NAME = "browser";

export type PanelType = "chat" | "iframe" | "subagent" | "launcher";

/** What a panel is showing. The same object is shared between the mounted
 *  view's bookkeeping (which the autosave serializes) and the live surface
 *  (which renders it), so an agent-driven url or title change reaches both at
 *  once. */
export interface PanelParams {
  panelType: PanelType;
  agentId: string;
  chatAgentId?: string;
  url?: string;
  title?: string;
  // LEGACY, read-only. The name a rename used to write onto the panel, back
  // when a name was kept with the tab showing an object and therefore in one
  // view's saved layout. Names are filed by ref now (see models/MemberTitles),
  // so nothing writes this any more -- but a layout saved before the change is
  // still carrying one, and losing a name on upgrade would be worse than
  // reading a field nobody sets. It is the last fallback behind the store, so a
  // rename since the upgrade always wins over it.
  customTitle?: string;
  subagentSessionId?: string;
  // Workspace service name this iframe is tied to (e.g. "web", "api").
  // Set only for iframe tabs that proxy an actual workspace service; left
  // undefined for ad-hoc URL tabs, terminals, and agent-owned iframes.
  // Drives both the WS-driven `layout_op` (op="refresh") service-wide
  // reload match and the presence of the per-tab Refresh button.
  serviceName?: string;
  // Which instance of the service this pane shows: the canonical minted
  // instance name ("files-2"), which is the object's identity -- the pane's
  // live key and member ref are both built from it. Unset only transiently,
  // while a pane's instance is still being minted (or, for a pane restored
  // from a pre-instances layout, adopted); such a pane is keyed by its panel
  // until the name lands, exactly as a terminal is before its tmux session
  // name is allocated.
  serviceInstanceId?: string;
  // Set only on persistent-terminal iframe tabs. ``terminalSessionName`` is
  // the named tmux session the tab attaches to (attach-or-create); its
  // presence is what marks a panel as a terminal (drives the banner, the
  // Destroy button, and layout-restore reattach). ``terminalId`` is a
  // per-tab id passed into the ttyd URL so the backend can map this tab's
  // tmux client back to us for live title tracking. ``terminalSessionId`` is
  // the immutable ``#{session_id}`` used to reflect a rename onto the tab.
  terminalSessionName?: string;
  terminalId?: string;
  terminalSessionId?: string;
}

/** The identity a live page is filed under, machine-wide. */
export type LiveKey = string;

/** One live page: the element that holds it, what it renders, and whether any
 *  pane is currently showing it. The binding fields below it are owned by this
 *  module and must not be written from outside. */
export interface LiveSurface {
  /** The object identity this page is filed under. Follows the object when a
   *  terminal's tmux session name finally lands (see ``rekeyLiveSurface``). */
  key: LiveKey;
  /** The element that holds the page. Created once, appended to the live
   *  layer, and removed only when the object is destroyed. */
  readonly element: HTMLElement;
  /** What to render. Read on every redraw rather than captured, so a live
   *  ``replace-url`` or rename follows without remounting. */
  params: PanelParams;
  /** Whether a pane is showing this page right now. */
  isVisible: boolean;
  /** The slot currently standing in for this page, if any. */
  boundPanelId: string | null;
  boundApi: DockviewPanelApi | null;
  bindingDisposables: Array<{ dispose: () => void }>;
  unmount: () => void;
}

// While a tab is being dragged, surfaces stop taking pointer events so the
// drag lands on the slot overlay underneath -- which is what carries
// dockview's drop-target forwarding.
const SURFACE_DRAG_CLASS = "si-live-surface--drag";

const surfacesByKey = new Map<LiveKey, LiveSurface>();
let layerHost: HTMLElement | null = null;
let onVisibilityChanged: (() => void) | null = null;
let reconcileFrame: number | null = null;
let isDragInProgress = false;

// ---------- The key grammar (pure) ----------

/** The ``session`` id a URL addresses, or null when it names none. Parsed off
 *  the raw string rather than through ``new URL`` so it needs no ``location``
 *  to resolve a relative address against. */
export function sessionParamFromUrl(url: string | undefined): string | null {
  if (!url) return null;
  const queryIndex = url.indexOf("?");
  if (queryIndex === -1) return null;
  const hashIndex = url.indexOf("#", queryIndex);
  const query = hashIndex === -1 ? url.substring(queryIndex + 1) : url.substring(queryIndex + 1, hashIndex);
  return new URLSearchParams(query).get("session");
}

/**
 * The live page a panel stands for, or null for a panel that is not an object.
 *
 * Deliberately the member-ref grammar wherever an object has a durable
 * identity (``chat:<agent-id>``, ``terminal:<session>``), so the thing a
 * project files and the thing that holds its page are spelled the same way.
 * A service pane's key qualifies the ref with which page of the service it
 * shows: ``service:<name>?instance=<name>-<N>`` for an app instance (the
 * canonical minted name -- an app pane whose instance has not landed yet is
 * keyed by its panel, exactly as a terminal is before its tmux session name
 * is allocated), and ``service:browser?session=<name>`` for a fleet browser,
 * whose per-session identity is durable server-side rather than minted here.
 * Where the member ref is itself a hash of the panel id (``url:<hash>``), the
 * key is ``panel:<id>`` instead -- bijective with it, and, unlike it,
 * synchronous.
 *
 * A launcher is a question about a pane rather than an object on the machine,
 * so it has no page to keep alive and stays an ordinary dockview panel.
 */
export function liveKeyForPanel(panelId: string, params: PanelParams | undefined): LiveKey | null {
  if (params === undefined || params.panelType === "launcher") return null;
  if (params.panelType === "chat") {
    const chatAgentId = params.chatAgentId ?? params.agentId;
    return chatAgentId ? `chat:${chatAgentId}` : null;
  }
  if (params.terminalSessionName) return `terminal:${params.terminalSessionName}`;
  if (params.serviceName) {
    const session = params.serviceName === BROWSER_SERVICE_NAME ? sessionParamFromUrl(params.url) : null;
    if (session !== null) return `service:${BROWSER_SERVICE_NAME}?session=${session}`;
    if (params.serviceInstanceId) return `service:${params.serviceName}?instance=${params.serviceInstanceId}`;
  }
  return `panel:${panelId}`;
}

/** The live page a member ref stands for. Chat and terminal refs are already
 *  live keys, as are an app instance's and a browser's per-session service
 *  refs. A bare ``service:<name>`` ref is an app's pin rather than an object
 *  with a page, and an ad-hoc page is filed under a hash of its panel id;
 *  both answer to ``panel:<id>`` -- the caller always has the panel id, live
 *  or deterministic. */
export function liveKeyForRef(ref: string, panelId: string): LiveKey {
  if (ref.startsWith("chat:") || ref.startsWith("terminal:")) return ref;
  if (ref.startsWith("service:") && ref.includes("?")) return ref;
  return `panel:${panelId}`;
}

/** The panels to drop when a restored arrangement names one object twice. An
 *  object is a singleton, so two tabs would fight over one page; the first
 *  occurrence keeps it. Panels that are not objects (``null`` keys) are never
 *  deduped against each other. */
export function duplicateLiveKeyPanelIds(entries: readonly { panelId: string; key: LiveKey | null }[]): string[] {
  const seen = new Set<LiveKey>();
  const duplicates: string[] = [];
  for (const entry of entries) {
    if (entry.key === null) continue;
    if (seen.has(entry.key)) {
      duplicates.push(entry.panelId);
      continue;
    }
    seen.add(entry.key);
  }
  return duplicates;
}

// ---------- The registry ----------

/**
 * Point the registry at the layer its surfaces live in, and at what to call
 * when a page starts or stops being looked at.
 *
 * The host is dockview's own overlay render container: the same parent and the
 * same coordinate space as the overlays dockview positions for its panels, so
 * the shipped pane clip applies to a surface verbatim and a surface sits
 * exactly where the panel's own content used to. It survives every ``clear()``
 * and ``fromJSON`` -- the gridview only ever replaces its root child -- which
 * is what lets a page outlive the panels that show it.
 */
export function initializeLiveLayer(host: HTMLElement, visibilityListener: () => void): void {
  layerHost = host;
  onVisibilityChanged = visibilityListener;
}

/** What ``key``'s page is rendering, or null when it has none. The restore
 *  path asks so it can leave a live page's url alone instead of rebuilding it
 *  (which would change the iframe's ``src``, i.e. reload it). */
export function liveSurfaceParams(key: LiveKey): PanelParams | null {
  return surfacesByKey.get(key)?.params ?? null;
}

/** The live page's DOM element for ``key``, or null when it has none. For callers that
 *  address the page's content (e.g. granting a just-activated terminal focus) without
 *  owning the surface. */
export function liveSurfaceElement(key: LiveKey): HTMLElement | null {
  return surfacesByKey.get(key)?.element ?? null;
}

/**
 * The page for ``key``, creating it on first open.
 *
 * ``mountContent`` runs exactly once per object, ever: the mount outlives
 * every pane that shows it, so an existing page is handed back untouched --
 * same document, same params, same scroll position -- no matter what the
 * caller was about to render into it.
 */
export function ensureLiveSurface(
  key: LiveKey,
  params: PanelParams,
  mountContent: (surface: LiveSurface) => void,
): LiveSurface {
  const existing = surfacesByKey.get(key);
  if (existing !== undefined) return existing;
  if (layerHost === null) {
    throw new Error("dockview: a live page was opened before the live layer was initialized");
  }
  const element = document.createElement("div");
  // Same class as the overlays dockview positions for its own panels, so the
  // shipped ``.dv-render-overlay`` pane clip applies without restating it.
  element.className = `dv-render-overlay si-live-surface${isDragInProgress ? ` ${SURFACE_DRAG_CLASS}` : ""}`;
  element.style.display = "none";
  const surface: LiveSurface = {
    key,
    element,
    params,
    isVisible: false,
    boundPanelId: null,
    boundApi: null,
    bindingDisposables: [],
    unmount: () => {
      m.mount(element, null);
    },
  };
  surfacesByKey.set(key, surface);
  layerHost.appendChild(element);
  mountContent(surface);
  return surface;
}

/** Re-file a page under a new identity without touching the page. A terminal
 *  opened before its tmux session name has been allocated starts out as
 *  ``panel:<id>`` and becomes ``terminal:<name>`` when the name lands; a
 *  rename moves it again. */
export function rekeyLiveSurface(fromKey: LiveKey, toKey: LiveKey): void {
  if (fromKey === toKey) return;
  const surface = surfacesByKey.get(fromKey);
  if (surface === undefined) return;
  surfacesByKey.delete(fromKey);
  surface.key = toKey;
  surfacesByKey.set(toKey, surface);
}

/** Show ``surface`` in the pane the slot ``panelId`` stands in for. The slot
 *  element itself is never read: the geometry is measured off the pane, so all
 *  the binding needs is the panel's api. */
export function bindSlot(surface: LiveSurface, panelId: string, api: DockviewPanelApi): void {
  releaseBinding(surface);
  surface.boundPanelId = panelId;
  surface.boundApi = api;
  surface.bindingDisposables.push(
    api.onDidVisibilityChange(() => scheduleReconcile()),
    api.onDidDimensionsChange(() => scheduleReconcile()),
  );
  scheduleReconcile();
}

/**
 * Stop showing whatever page ``panelId``'s slot was standing in for.
 *
 * Deliberately does not hide anything synchronously: a view switch unbinds
 * every outgoing slot and binds the incoming ones inside a single task, and
 * only the trailing reconcile paints -- so a page both views show never
 * blinks, and never moves through an intermediate position.
 */
export function unbindSlot(panelId: string): void {
  for (const surface of surfacesByKey.values()) {
    if (surface.boundPanelId !== panelId) continue;
    releaseBinding(surface);
    scheduleReconcile();
    return;
  }
}

/** Tear a page down. The only path that takes an element out of the DOM, and
 *  it exists for exactly one reason: the object behind the page is gone from
 *  the machine. */
export function destroyLiveSurface(key: LiveKey): void {
  const surface = surfacesByKey.get(key);
  if (surface === undefined) return;
  surfacesByKey.delete(key);
  releaseBinding(surface);
  surface.unmount();
  surface.element.remove();
}

/** Step every surface out of the way of an in-flight tab drag, or back into
 *  it. Without this the drop would land inside a framed page rather than on
 *  the pane's drop target. */
export function setDragInProgress(active: boolean): void {
  if (isDragInProgress === active) return;
  isDragInProgress = active;
  for (const surface of surfacesByKey.values()) {
    surface.element.classList.toggle(SURFACE_DRAG_CLASS, active);
  }
}

function releaseBinding(surface: LiveSurface): void {
  for (const disposable of surface.bindingDisposables) {
    disposable.dispose();
  }
  surface.bindingDisposables.length = 0;
  surface.boundPanelId = null;
  surface.boundApi = null;
}

/** Ask for a reconcile on the next frame. Safe to call from anywhere that
 *  might have moved, resized, shown or hidden a pane. */
export function scheduleReconcile(): void {
  if (reconcileFrame !== null) return;
  reconcileFrame = requestAnimationFrame(() => {
    reconcileFrame = null;
    reconcileLiveSurfaces();
  });
}

/**
 * Put every page where its pane is, and hide the ones nothing is showing.
 *
 * Hiding is ``display: none``, which is byte-for-byte what dockview already
 * does to an inactive tab: the content stays in the DOM, so the document keeps
 * running and keeps its scroll position. The only thing that changes here is
 * how long a page may stay hidden -- a closed tab, or a project that does not
 * include the object, rather than a tab switch.
 */
export function reconcileLiveSurfaces(): void {
  let visibilityChanged = false;
  for (const surface of surfacesByKey.values()) {
    const rect = slotRect(surface);
    const style = surface.element.style;
    if (rect === null) {
      style.display = "none";
    } else {
      style.left = `${rect.left}px`;
      style.top = `${rect.top}px`;
      style.width = `${rect.width}px`;
      style.height = `${rect.height}px`;
      // The pane's own corners, not a fixed radius: the card squares its
      // top-left while the leftmost tab is the active one, and a page clipped
      // to a rounded corner over that square one shows the card's white
      // through the gap -- obvious under anything dark, like a terminal.
      // Copying the card's whole shorthand keeps the two in step whatever the
      // rule decides, rather than restating the condition here.
      style.borderRadius = rect.radius;
      style.display = "";
    }
    if (surface.isVisible !== (rect !== null)) {
      surface.isVisible = rect !== null;
      visibilityChanged = true;
    }
  }
  if (!visibilityChanged) return;
  // A page that just appeared or disappeared has to re-run its own lifecycle
  // against the new visibility -- in particular a chat restoring the scroll
  // position it was holding while hidden.
  m.redraw();
  onVisibilityChanged?.();
}

/**
 * Where a page's pane is, in the live layer's coordinates, or null when
 * nothing is showing it.
 *
 * Measured off the pane's own content container -- the very element dockview
 * positions its overlays against -- rather than off the overlay, whose inline
 * rect is only written on the next frame. So a page shown by the view being
 * switched to is already in the right place on the first paint, instead of
 * spending a frame at the position the outgoing view left it in.
 */
function slotRect(
  surface: LiveSurface,
): { left: number; top: number; width: number; height: number; radius: string } | null {
  const api = surface.boundApi;
  if (api === null || layerHost === null || !api.isVisible) return null;
  const pane = api.group.element.querySelector<HTMLElement>(":scope > .dv-content-container");
  if (pane === null) return null;
  const box = pane.getBoundingClientRect();
  // A zero-sized pane is a dock that has not been laid out yet. Showing a page
  // there would hand a framed terminal a zero-column viewport to fit itself to.
  if (box.width <= 0 || box.height <= 0) return null;
  const host = layerHost.getBoundingClientRect();
  return {
    left: box.left - host.left,
    top: box.top - host.top,
    width: box.width,
    height: box.height,
    radius: getComputedStyle(pane).borderRadius,
  };
}
