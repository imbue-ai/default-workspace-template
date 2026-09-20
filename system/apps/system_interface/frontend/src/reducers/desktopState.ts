/**
 * The desktop's state and the reducers over it (desktop-interface plan section 6.2): one frozen
 * record of what this client knows (the apps, the desktops, the active desktop, this client's
 * layout of it, the render modes), and every verb of plan section 4 as a pure
 * ``(state, event) -> state`` step. The store applies these, schedules redraws, and saves when a
 * step marked the layout dirty; nothing here reads the DOM or the network.
 */

import type { AppRecord, Desktop, Frame, Layout, Placement, WindowRecord, WindowState } from "../model/records";
import { EMPTY_LAYOUT } from "../model/records";
import {
  effectivePlacements,
  focusedWindowId,
  withWindowFrame,
  withWindowMinimized,
  withWindowPlacedOnOpen,
  withWindowRaised,
  withWindowRestored,
  withWindowState,
  withoutPlacement,
} from "../geometry/stack";
import type { RenderModes } from "../theme/metrics";

export interface DesktopState {
  readonly clientId: string;
  readonly apps: readonly AppRecord[];
  /** False until the first ``apps_updated`` has arrived, so an empty list is not read as "no apps". */
  readonly isAppsLoaded: boolean;
  readonly desktops: readonly Desktop[];
  readonly isDesktopsLoaded: boolean;
  readonly activeDesktopId: string | null;
  /** This client's layout of the active desktop. */
  readonly layout: Layout;
  /** False between a desktop switch and the fetch of its layout answering. */
  readonly isLayoutLoaded: boolean;
  /** Bumped by every gesture that changed the layout; a save carries the version it wrote. */
  readonly layoutVersion: number;
  /** The version the last save wrote, so ``isLayoutDirty`` is a comparison rather than a flag. */
  readonly savedLayoutVersion: number;
  readonly modes: RenderModes;
}

export function initialDesktopState(clientId: string, modes: RenderModes): DesktopState {
  return {
    clientId,
    apps: [],
    isAppsLoaded: false,
    desktops: [],
    isDesktopsLoaded: false,
    activeDesktopId: null,
    layout: EMPTY_LAYOUT,
    isLayoutLoaded: false,
    layoutVersion: 0,
    savedLayoutVersion: 0,
    modes,
  };
}

export type DesktopEvent =
  | { readonly type: "apps_updated"; readonly apps: readonly AppRecord[] }
  | { readonly type: "desktops_updated"; readonly desktops: readonly Desktop[] }
  | { readonly type: "desktop_activated"; readonly desktopId: string }
  | { readonly type: "layout_loaded"; readonly desktopId: string; readonly layout: Layout }
  | {
      readonly type: "layout_saved";
      readonly desktopId: string;
      readonly version: number;
      readonly updatedAt: string | null;
    }
  | { readonly type: "window_raised"; readonly windowId: string }
  | { readonly type: "window_minimized"; readonly windowId: string }
  | { readonly type: "window_restored"; readonly windowId: string }
  | { readonly type: "window_state_set"; readonly windowId: string; readonly state: WindowState }
  | { readonly type: "window_frame_set"; readonly windowId: string; readonly frame: Frame }
  | {
      readonly type: "window_opened_here";
      readonly desktopId: string;
      readonly window: WindowRecord;
      readonly isNew: boolean;
    }
  | { readonly type: "window_closed_here"; readonly desktopId: string; readonly windowId: string }
  | { readonly type: "render_modes_changed"; readonly modes: RenderModes };

/** Whether a gesture has changed the layout since the last save wrote it. */
export function isLayoutDirty(state: DesktopState): boolean {
  return state.layoutVersion !== state.savedLayoutVersion;
}

function withLayoutEdited(state: DesktopState, layout: Layout): DesktopState {
  if (layout === state.layout) return state;
  return { ...state, layout, layoutVersion: state.layoutVersion + 1 };
}

/** The state after a switch to ``desktopId``: the layout unknown until it is fetched. */
function withDesktopActivated(state: DesktopState, desktopId: string): DesktopState {
  if (state.activeDesktopId === desktopId) return state;
  return {
    ...state,
    activeDesktopId: desktopId,
    layout: EMPTY_LAYOUT,
    isLayoutLoaded: false,
    layoutVersion: state.layoutVersion + 1,
    savedLayoutVersion: state.layoutVersion + 1,
  };
}

function withDesktopsUpdated(state: DesktopState, desktops: readonly Desktop[]): DesktopState {
  const next: DesktopState = { ...state, desktops, isDesktopsLoaded: true };
  // A client whose desktop was deleted lands on the first remaining one (plan section 3.5).
  const isActiveGone =
    next.activeDesktopId !== null && !desktops.some((desktop) => desktop.id === next.activeDesktopId);
  if (isActiveGone && desktops.length > 0) return withDesktopActivated(next, desktops[0].id);
  return next;
}

function withWindowOpenedHere(
  state: DesktopState,
  desktopId: string,
  window: WindowRecord,
  isNew: boolean,
): DesktopState {
  const desktops = state.desktops.map((desktop) => {
    if (desktop.id !== desktopId || desktop.windows.some((candidate) => candidate.id === window.id)) return desktop;
    return { ...desktop, windows: [...desktop.windows, window] };
  });
  const withWindow: DesktopState = { ...state, desktops };
  if (desktopId !== state.activeDesktopId) return withWindow;
  // The shell already wrote this placement; the local copy is what the broadcast will confirm, so
  // it is not counted as a gesture to save.
  const layout = isNew ? withWindowPlacedOnOpen(state.layout, window.id) : withWindowRaised(state.layout, window.id);
  return { ...withWindow, layout };
}

function withWindowClosedHere(state: DesktopState, desktopId: string, windowId: string): DesktopState {
  const desktops = state.desktops.map((desktop) =>
    desktop.id === desktopId
      ? { ...desktop, windows: desktop.windows.filter((candidate) => candidate.id !== windowId) }
      : desktop,
  );
  const layout = desktopId === state.activeDesktopId ? withoutPlacement(state.layout, windowId) : state.layout;
  return { ...state, desktops, layout };
}

export function reduceDesktopState(state: DesktopState, event: DesktopEvent): DesktopState {
  switch (event.type) {
    case "apps_updated":
      return { ...state, apps: event.apps, isAppsLoaded: true };
    case "desktops_updated":
      return withDesktopsUpdated(state, event.desktops);
    case "desktop_activated":
      return withDesktopActivated(state, event.desktopId);
    case "layout_loaded":
      if (event.desktopId !== state.activeDesktopId) return state;
      return {
        ...state,
        layout: event.layout,
        isLayoutLoaded: true,
        layoutVersion: state.layoutVersion + 1,
        savedLayoutVersion: state.layoutVersion + 1,
      };
    case "layout_saved": {
      if (event.desktopId !== state.activeDesktopId) return state;
      const layout = event.updatedAt === null ? state.layout : { ...state.layout, updated_at: event.updatedAt };
      return { ...state, layout, savedLayoutVersion: event.version };
    }
    case "window_raised":
      return withLayoutEdited(state, withWindowRaised(state.layout, event.windowId));
    case "window_minimized":
      return withLayoutEdited(state, withWindowMinimized(state.layout, event.windowId));
    case "window_restored":
      return withLayoutEdited(state, withWindowRestored(state.layout, event.windowId));
    case "window_state_set":
      return withLayoutEdited(state, withWindowState(state.layout, event.windowId, event.state));
    case "window_frame_set":
      return withLayoutEdited(state, withWindowFrame(state.layout, event.windowId, event.frame));
    case "window_opened_here":
      return withWindowOpenedHere(state, event.desktopId, event.window, event.isNew);
    case "window_closed_here":
      return withWindowClosedHere(state, event.desktopId, event.windowId);
    case "render_modes_changed":
      return { ...state, modes: event.modes };
  }
}

export function activeDesktop(state: DesktopState): Desktop | null {
  return state.desktops.find((desktop) => desktop.id === state.activeDesktopId) ?? null;
}

export function desktopById(state: DesktopState, desktopId: string): Desktop | null {
  return state.desktops.find((desktop) => desktop.id === desktopId) ?? null;
}

export function appByName(state: DesktopState, name: string): AppRecord | undefined {
  return state.apps.find((app) => app.name === name);
}

/** The apps a user can open from: every non-internal row, in registry order. */
export function openableApps(state: DesktopState): AppRecord[] {
  return state.apps.filter((app) => !app.internal);
}

/** A window of any desktop, with the desktop it is on. */
export function findWindow(state: DesktopState, windowId: string): { desktop: Desktop; window: WindowRecord } | null {
  for (const desktop of state.desktops) {
    const window = desktop.windows.find((candidate) => candidate.id === windowId);
    if (window !== undefined) return { desktop, window };
  }
  return null;
}

/** Every window of the active desktop placed, back to front. */
export function activePlacements(state: DesktopState): Placement[] {
  const desktop = activeDesktop(state);
  return desktop === null ? [] : effectivePlacements(state.layout, desktop);
}

export function activeFocusedWindowId(state: DesktopState): string | null {
  return focusedWindowId(activePlacements(state));
}

/** The state a placement renders in: maximized whatever it says while compact (never rewritten). */
export function renderedState(placement: Placement, modes: RenderModes): WindowState {
  return modes.isCompact ? "MAXIMIZED" : placement.state;
}

/** The title a window shows: what its page reported, else its app's display name. */
export function windowTitle(window: WindowRecord, app: AppRecord | undefined): string {
  if (window.title !== "") return window.title;
  return app?.display_name ?? window.app;
}

/** Whether the workspace can stop and start this app: supervised, not critical to the workspace,
 *  and not running inside a critical app's program (the shell refuses those the same way). */
export function isAppStoppable(state: DesktopState, app: AppRecord): boolean {
  if (app.program === "" || app.critical) return false;
  return !state.apps.some((other) => other.critical && other.program === app.program);
}

/** One entry of the taskbar: a window of the active desktop, in opening order. */
export interface TaskbarEntry {
  readonly window: WindowRecord;
  readonly app: AppRecord | undefined;
  readonly title: string;
  readonly isMinimized: boolean;
  readonly isFocused: boolean;
}

export function taskbarEntries(state: DesktopState): TaskbarEntry[] {
  const desktop = activeDesktop(state);
  if (desktop === null) return [];
  const placements = effectivePlacements(state.layout, desktop);
  const focused = focusedWindowId(placements);
  const placementById = new Map(placements.map((placement) => [placement.window_id, placement]));
  return desktop.windows.map((window) => {
    const app = appByName(state, window.app);
    return {
      window,
      app,
      title: windowTitle(window, app),
      isMinimized: placementById.get(window.id)?.is_minimized ?? true,
      isFocused: window.id === focused,
    };
  });
}
