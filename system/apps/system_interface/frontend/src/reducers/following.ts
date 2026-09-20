/**
 * Following the URL (desktop-interface plan section 4.6, contracts.md section 7): after every
 * ``desktops_updated``, each live page whose window's stored path differs from the path the page
 * last reported (or was last pointed at) is navigated -- in place, when the page declared it
 * handles navigation, else by reloading its frame. A page whose last report equals the stored
 * path is left alone, which is how the driving client's own report never bounces back.
 */

import type { WindowRecord } from "../model/records";

/** What the live-page layer remembers about one page. */
export interface PageReport {
  /** The path the page last reported, or was last pointed at; null for a page told nothing yet. */
  readonly lastReportedPath: string | null;
  /** Whether the page declared ``navigation: true`` in its capabilities. */
  readonly isNavigationCapable: boolean;
}

export type FollowMode = "navigate" | "reload";

export interface FollowAction {
  readonly windowId: string;
  readonly path: string;
  readonly mode: FollowMode;
}

/** The pages to move so they show their windows' stored paths, and how. */
export function navigationsToFollow(
  windows: readonly WindowRecord[],
  reportByWindowId: ReadonlyMap<string, PageReport>,
): FollowAction[] {
  const actions: FollowAction[] = [];
  for (const window of windows) {
    const report = reportByWindowId.get(window.id);
    if (report === undefined || report.lastReportedPath === window.path) continue;
    actions.push({ windowId: window.id, path: window.path, mode: report.isNavigationCapable ? "navigate" : "reload" });
  }
  return actions;
}
