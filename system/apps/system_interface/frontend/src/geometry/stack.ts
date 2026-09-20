/**
 * The stack (desktop-interface plan section 3.4): one client's placements of one desktop, back to
 * front, and the verbs over them, mirroring ``shell/desktop_document.py``. A window the layout
 * has no placement for reads as minimized at the bottom of the stack with a cascaded frame; the
 * focused window is the last placement that is not minimized. Nothing here reads the DOM.
 */

import type { Desktop, Frame, Layout, Placement, WindowRecord, WindowState } from "../model/records";
import { cascadeFrame } from "./frames";

/** What a window with no placement reads as: the cascade frame at the bottom of the stack, minimized. */
export function defaultPlacement(windowId: string, storedCount: number): Placement {
  return { window_id: windowId, frame: cascadeFrame(storedCount), state: "NORMAL", is_minimized: true };
}

/** The placement an open writes for the requesting client: the cascade frame, normal, shown. */
export function openedPlacement(windowId: string, storedCount: number): Placement {
  return { window_id: windowId, frame: cascadeFrame(storedCount), state: "NORMAL", is_minimized: false };
}

/** The layout without every placement naming a window the desktop no longer holds. */
export function dropStalePlacements(layout: Layout, liveWindowIds: ReadonlySet<string>): Layout {
  const kept = layout.placements.filter((placement) => liveWindowIds.has(placement.window_id));
  return kept.length === layout.placements.length ? layout : { ...layout, placements: kept };
}

/** Every window of the desktop placed: the stored placements in their order (stale ones dropped), with the
 *  windows the layout lacks read as the default placement at the start of the list, in opening order. */
export function effectivePlacements(layout: Layout, desktop: Desktop): Placement[] {
  const liveIds = new Set(desktop.windows.map((window) => window.id));
  const stored = dropStalePlacements(layout, liveIds).placements;
  const storedIds = new Set(stored.map((placement) => placement.window_id));
  const missing = desktop.windows
    .filter((window) => !storedIds.has(window.id))
    .map((window) => defaultPlacement(window.id, stored.length));
  return [...missing, ...stored];
}

/** The last placement that is not minimized; null when the backdrop has focus. */
export function focusedWindowId(placements: readonly Placement[]): string | null {
  for (let index = placements.length - 1; index >= 0; index -= 1) {
    if (!placements[index].is_minimized) return placements[index].window_id;
  }
  return null;
}

/** The window of ``app`` nearest the top of this client's stack, minimized or not, or null. */
export function mostRecentlyFocusedWindowOfApp(layout: Layout, desktop: Desktop, app: string): WindowRecord | null {
  const windowsById = new Map(desktop.windows.map((window) => [window.id, window]));
  const placements = effectivePlacements(layout, desktop);
  for (let index = placements.length - 1; index >= 0; index -= 1) {
    const window = windowsById.get(placements[index].window_id);
    if (window !== undefined && window.app === app) return window;
  }
  return null;
}

/** The window's stored placement, else its default. */
export function placementOf(layout: Layout, windowId: string): Placement {
  return (
    layout.placements.find((placement) => placement.window_id === windowId) ??
    defaultPlacement(windowId, layout.placements.length)
  );
}

function withPlacementOnTop(layout: Layout, placement: Placement): Layout {
  const others = layout.placements.filter((candidate) => candidate.window_id !== placement.window_id);
  return { ...layout, placements: [...others, placement] };
}

/** The layout with the placement replacing its window's entry where it stands, or appended when there is none. */
function withPlacementInPlace(layout: Layout, placement: Placement): Layout {
  if (!layout.placements.some((candidate) => candidate.window_id === placement.window_id)) {
    return { ...layout, placements: [...layout.placements, placement] };
  }
  return {
    ...layout,
    placements: layout.placements.map((candidate) =>
      candidate.window_id === placement.window_id ? placement : candidate,
    ),
  };
}

/** The layout with a just-opened window on top of the stack, at the cascade frame, shown. */
export function withWindowPlacedOnOpen(layout: Layout, windowId: string): Layout {
  return withPlacementOnTop(layout, openedPlacement(windowId, layout.placements.length));
}

/** Focus: the window restored (un-minimized) and moved to the top of the stack. */
export function withWindowRaised(layout: Layout, windowId: string): Layout {
  return withPlacementOnTop(layout, { ...placementOf(layout, windowId), is_minimized: false });
}

/** Minimize: the window out of sight where it stands in the stack. */
export function withWindowMinimized(layout: Layout, windowId: string): Layout {
  return withPlacementInPlace(layout, { ...placementOf(layout, windowId), is_minimized: true });
}

/** Restore: the window shown at its own frame, normal, on top of the stack. */
export function withWindowRestored(layout: Layout, windowId: string): Layout {
  return withPlacementOnTop(layout, { ...placementOf(layout, windowId), is_minimized: false, state: "NORMAL" });
}

/** Maximize or snap: the state set with the frame untouched, the window shown and raised. */
export function withWindowState(layout: Layout, windowId: string, state: WindowState): Layout {
  return withPlacementOnTop(layout, { ...placementOf(layout, windowId), is_minimized: false, state });
}

/** Place at a frame: the frame set with the state normal, the window shown and raised. */
export function withWindowFrame(layout: Layout, windowId: string, frame: Frame): Layout {
  return withPlacementOnTop(layout, { ...placementOf(layout, windowId), is_minimized: false, state: "NORMAL", frame });
}

/** The layout without the window's placement. */
export function withoutPlacement(layout: Layout, windowId: string): Layout {
  if (!layout.placements.some((candidate) => candidate.window_id === windowId)) return layout;
  return { ...layout, placements: layout.placements.filter((candidate) => candidate.window_id !== windowId) };
}
