/**
 * The live pages, and which pane is showing each one.
 *
 * There is ONE live page per instance, machine-wide: an instance open in three projects is
 * one iframe and one document, not three. A project is a *view* that may or may not include
 * the instance, and a pane is only a place to show it at some size. Removing an iframe from
 * the document destroys it, and re-parenting one reloads it, so the element holding a page
 * must never leave the DOM -- not on a view switch, not on a tab close, not on a re-arrange.
 * The instance leaving its app's list is the one thing that takes it out.
 *
 * So this registry is global and keyed by the instance's address, and a dockview panel is
 * demoted to a **slot**: an empty div that dockview creates, positions, hides and disposes at
 * will. A surface lives in the same layer dockview positions its own panel overlays in,
 * mirrors the geometry and visibility of whichever slot currently shows it, and outlives
 * every one of them.
 *
 * DockviewWorkspace owns everything that knows what a page *is* -- which url it loads, what it
 * is called, what filing it means. This module owns only where the live elements are, how
 * big, and whether anything is looking at them.
 */

import m from "mithril";
import type { DockviewPanelApi } from "dockview-core";

/** What a panel is showing: an instance, by address, in the tab the shell minted for it; or
 *  the New Tab launcher, which is a question about a pane rather than an instance. The same
 *  object is shared between the mounted view's bookkeeping (which the autosave serializes) and
 *  the live surface (which renders it), so a rebind reaches both at once. */
export type PanelParams = { kind: "instance"; address: string; tabId: string } | { kind: "launcher" };

/** The identity a live page is filed under: the instance's address. */
export type LiveKey = string;

/** One live page: the element that holds it, what it renders, and whether any pane is
 *  currently showing it. The binding fields below it are owned by this module. */
export interface LiveSurface {
  /** The address this page is filed under. Follows the instance when a tab is rebound to
   *  another one of the same app (see ``rekeyLiveSurface``). */
  key: LiveKey;
  /** The element that holds the page. Created once, appended to the live layer, and removed
   *  only when the instance is gone. */
  readonly element: HTMLElement;
  /** What to render. Read on every redraw rather than captured, so a rebind follows without
   *  remounting. */
  params: PanelParams;
  /** Whether a pane is showing this page right now. */
  isVisible: boolean;
  /** The path the page itself last reported (``shell:location``), cleared by the frame's next
   *  load. A listed url equal to it is where the page already is, so no reload. */
  lastReportedPath: string | null;
  /** The slot currently standing in for this page, if any. */
  boundPanelId: string | null;
  boundApi: DockviewPanelApi | null;
  bindingDisposables: Array<{ dispose: () => void }>;
  unmount: () => void;
}

// While a tab is being dragged, surfaces stop taking pointer events so the drag lands on the
// slot overlay underneath -- which is what carries dockview's drop-target forwarding.
const SURFACE_DRAG_CLASS = "si-live-surface--drag";

const surfacesByKey = new Map<LiveKey, LiveSurface>();
let layerHost: HTMLElement | null = null;
let onVisibilityChanged: (() => void) | null = null;
let reconcileFrame: number | null = null;
let isDragInProgress = false;

/** The live page a panel stands for: the instance's address, or null for a launcher. */
export function liveKeyForPanel(params: PanelParams | undefined): LiveKey | null {
  if (params === undefined || params.kind === "launcher") return null;
  return params.address;
}

/** The panels to drop when a restored arrangement names one instance twice. An instance is a
 *  singleton, so two tabs would fight over one page; the first occurrence keeps it. Panels
 *  that are not instances (``null`` keys) are never deduped against each other. */
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
 * Point the registry at the layer its surfaces live in, and at what to call when a page
 * starts or stops being looked at.
 *
 * The host is dockview's own overlay render container: the same parent and the same
 * coordinate space as the overlays dockview positions for its panels, so the shipped pane
 * clip applies to a surface verbatim. It survives every ``clear()`` and ``fromJSON`` -- the
 * gridview only ever replaces its root child -- which is what lets a page outlive the panels
 * that show it.
 */
export function initializeLiveLayer(host: HTMLElement, visibilityListener: () => void): void {
  layerHost = host;
  onVisibilityChanged = visibilityListener;
}

/** The panel currently standing in for the page filed under ``key``, or null when no pane shows it. */
export function liveSurfaceBoundPanelId(key: LiveKey): string | null {
  return surfacesByKey.get(key)?.boundPanelId ?? null;
}

/** The live page's DOM element for ``key``, or null when it has none. */
export function liveSurfaceElement(key: LiveKey): HTMLElement | null {
  return surfacesByKey.get(key)?.element ?? null;
}

/** Every address a live page is currently filed under. */
export function liveSurfaceKeys(): LiveKey[] {
  return Array.from(surfacesByKey.keys());
}

/**
 * The page for ``key``, creating it on first open.
 *
 * ``mountContent`` runs exactly once per instance, ever: the mount outlives every pane that
 * shows it, so an existing page is handed back untouched -- same document, same params, same
 * scroll position -- no matter what the caller was about to render into it.
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
  // Same class as the overlays dockview positions for its own panels, so the shipped
  // ``.dv-render-overlay`` pane clip applies without restating it.
  element.className = `dv-render-overlay si-live-surface${isDragInProgress ? ` ${SURFACE_DRAG_CLASS}` : ""}`;
  element.style.display = "none";
  const surface: LiveSurface = {
    key,
    element,
    params,
    isVisible: false,
    lastReportedPath: null,
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

/** Remember the path a page reported for itself, so the record catching up with it is not a reload. */
export function recordReportedPath(key: LiveKey, path: string): void {
  const surface = surfacesByKey.get(key);
  if (surface !== undefined) surface.lastReportedPath = path;
}

/** A fresh page has reported nothing: the frame's load forgets the last report. */
export function clearReportedPath(key: LiveKey): void {
  const surface = surfacesByKey.get(key);
  if (surface !== undefined) surface.lastReportedPath = null;
}

/** Whether the page is already at ``listedUrl``: its path (query and fragment included) is
 *  exactly what the page last reported, so the record merely caught up with the page. An
 *  agent's replace-url names another path and still lands (contracts.md section 10). */
export function isPageAtListedUrl(listedUrl: string, lastReportedPath: string | null): boolean {
  if (lastReportedPath === null) return false;
  const parsed = new URL(listedUrl, "http://placeholder.invalid");
  return `${parsed.pathname}${parsed.search}${parsed.hash}` === lastReportedPath;
}

/** Re-file a page under a new address without touching the page: a tab whose app re-pointed
 *  it at another instance (a terminal's client switching tmux session) keeps its iframe. An
 *  instance has one page, so a page already filed under the new address goes. */
export function rekeyLiveSurface(fromKey: LiveKey, toKey: LiveKey): void {
  if (fromKey === toKey) return;
  const surface = surfacesByKey.get(fromKey);
  if (surface === undefined) return;
  destroyLiveSurface(toKey);
  surfacesByKey.delete(fromKey);
  surface.key = toKey;
  surfacesByKey.set(toKey, surface);
}

/** Show ``surface`` in the pane the slot ``panelId`` stands in for. */
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
 * Deliberately does not hide anything synchronously: a view switch unbinds every outgoing slot
 * and binds the incoming ones inside a single task, and only the trailing reconcile paints --
 * so a page both views show never blinks, and never moves through an intermediate position.
 */
export function unbindSlot(panelId: string): void {
  for (const surface of surfacesByKey.values()) {
    if (surface.boundPanelId !== panelId) continue;
    releaseBinding(surface);
    scheduleReconcile();
    return;
  }
}

/** Tear a page down. The only path that takes an element out of the DOM, and it exists for
 *  exactly one reason: the instance behind the page is gone from its app's list. */
export function destroyLiveSurface(key: LiveKey): void {
  const surface = surfacesByKey.get(key);
  if (surface === undefined) return;
  surfacesByKey.delete(key);
  releaseBinding(surface);
  surface.unmount();
  surface.element.remove();
}

/** Step every surface out of the way of an in-flight tab drag, or back into it. Without this
 *  the drop would land inside a framed page rather than on the pane's drop target. */
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

/** Ask for a reconcile on the next frame. Safe to call from anywhere that might have moved,
 *  resized, shown or hidden a pane. */
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
 * Hiding is ``display: none``, which is byte-for-byte what dockview already does to an
 * inactive tab: the content stays in the DOM, so the document keeps running and keeps its
 * scroll position.
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
      // The pane's own corners, not a fixed radius: the card squares its top-left while the
      // leftmost tab is the active one, and a page clipped to a rounded corner over that
      // square one shows the card's white through the gap.
      style.borderRadius = rect.radius;
      style.display = "";
    }
    if (surface.isVisible !== (rect !== null)) {
      surface.isVisible = rect !== null;
      visibilityChanged = true;
    }
  }
  if (!visibilityChanged) return;
  // A page that just appeared or disappeared is told so (the frames redraw and send shown or
  // hidden), and a chat's presence follows.
  m.redraw();
  onVisibilityChanged?.();
}

/**
 * Where a page's pane is, in the live layer's coordinates, or null when nothing is showing it.
 *
 * Measured off the pane's own content container -- the very element dockview positions its
 * overlays against -- rather than off the overlay, whose inline rect is only written on the
 * next frame. So a page shown by the view being switched to is already in the right place on
 * the first paint.
 */
function slotRect(
  surface: LiveSurface,
): { left: number; top: number; width: number; height: number; radius: string } | null {
  const api = surface.boundApi;
  if (api === null || layerHost === null || !api.isVisible) return null;
  const pane = api.group.element.querySelector<HTMLElement>(":scope > .dv-content-container");
  if (pane === null) return null;
  const box = pane.getBoundingClientRect();
  // A zero-sized pane is a dock that has not been laid out yet. Showing a page there would
  // hand a framed terminal a zero-column viewport to fit itself to.
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
