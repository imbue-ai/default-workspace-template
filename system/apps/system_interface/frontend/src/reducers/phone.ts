/**
 * What the phone layout reads off the desktop's state (plan-phone-interface.md), as pure selectors. Desktops are
 * not a phone concept, so every selector but the pinned window's reads every desktop's windows as one list; the
 * client's active desktop only decides which of several candidates the phone prefers.
 */

import { mostRecentlyFocusedWindowOfApp } from "../geometry/stack";
import { appLaunchesOf, orderAppLaunches } from "../model/launch";
import type { AppRecord, WindowRecord } from "../model/records";
import { SHOWN_HOME } from "../model/records";
import { activeDesktop, appByName, effectiveWindowTitle, findWindow, openableApps } from "./desktopState";
import type { DesktopState, PhoneShown } from "./desktopState";

/** One row of the windows sheet: a window of some desktop, as the phone names it. */
export interface PhoneWindowRow {
  readonly window: WindowRecord;
  readonly app: AppRecord | undefined;
  readonly title: string;
}

/** The pinned window the phone keeps: the one on the client's active desktop (the shell keeps one per desktop). */
export function pinnedChatWindowOf(state: DesktopState): WindowRecord | null {
  return activeDesktop(state)?.windows.find((window) => window.is_pinned) ?? null;
}

/** Every window the phone lists: every desktop's, less the pinned windows of the desktops the client is not on,
 *  since the phone keeps one pinned window. */
function phoneWindows(state: DesktopState): WindowRecord[] {
  const pinned = pinnedChatWindowOf(state);
  return state.desktops.flatMap((desktop) =>
    desktop.windows.filter((window) => !window.is_pinned || window.id === pinned?.id),
  );
}

/** The window the phone shows, when it shows one that still exists. */
export function shownWindowOf(state: DesktopState): WindowRecord | null {
  const shown = state.phone.shown;
  if (shown === null || shown.kind !== "window") return null;
  return findWindow(state, shown.windowId)?.window ?? null;
}

/** Whether the phone shows a window no desktop holds any longer. */
export function isShownWindowGone(state: DesktopState): boolean {
  const shown = state.phone.shown;
  return shown !== null && shown.kind === "window" && findWindow(state, shown.windowId) === null;
}

/** Where a load lands: the newest recorded entry that still means something (a window the phone still lists, or
 *  the home grid), else the pinned chat window, else the home grid. */
export function phoneLanding(state: DesktopState): PhoneShown {
  const listedIds = new Set(phoneWindows(state).map((window) => window.id));
  for (let index = state.phone.history.length - 1; index >= 0; index -= 1) {
    const entry = state.phone.history[index];
    if (entry === SHOWN_HOME) return { kind: "home" };
    if (listedIds.has(entry)) return { kind: "window", windowId: entry };
  }
  const pinned = pinnedChatWindowOf(state);
  return pinned === null ? { kind: "home" } : { kind: "window", windowId: pinned.id };
}

function newestFirst(left: WindowRecord, right: WindowRecord): number {
  if (left.opened_at === right.opened_at) return 0;
  return left.opened_at < right.opened_at ? 1 : -1;
}

/** The windows sheet's rows: the windows this phone has shown, most recent first, then every other window, newest
 *  first. */
export function windowsSheetRows(state: DesktopState): PhoneWindowRow[] {
  const windows = phoneWindows(state);
  const byId = new Map(windows.map((window) => [window.id, window]));
  const recent = [...state.phone.history]
    .reverse()
    .map((entry) => byId.get(entry))
    .filter((window): window is WindowRecord => window !== undefined);
  const recentIds = new Set(recent.map((window) => window.id));
  const rest = windows.filter((window) => !recentIds.has(window.id)).sort(newestFirst);
  return [...recent, ...rest].map((window) => {
    const app = appByName(state, window.app);
    return { window, app, title: effectiveWindowTitle(state, window, app) };
  });
}

/** How many windows the pill counts: every window the sheet lists but the pinned one. */
export function openWindowCount(state: DesktopState): number {
  return phoneWindows(state).filter((window) => !window.is_pinned).length;
}

/** The home grid's apps: every openable app, in the order the launcher ranks their launch paths, then any app
 *  that declares none. */
export function homeGridApps(state: DesktopState): AppRecord[] {
  const apps = openableApps(state);
  const ordered: AppRecord[] = [];
  for (const { app } of orderAppLaunches(appLaunchesOf(apps))) {
    if (!ordered.includes(app)) ordered.push(app);
  }
  return [...ordered, ...apps.filter((app) => !ordered.includes(app))];
}

/** The window a home tile shows: the app's window nearest the top of this client's stack on its active desktop,
 *  else its newest window anywhere; null when it has none. */
export function focusTargetOf(state: DesktopState, appName: string): WindowRecord | null {
  const desktop = activeDesktop(state);
  const onActive = desktop === null ? null : mostRecentlyFocusedWindowOfApp(state.layout, desktop, appName);
  if (onActive !== null) return onActive;
  const windows = phoneWindows(state).filter((window) => window.app === appName);
  return windows.sort(newestFirst)[0] ?? null;
}

/** What the pill says: the workspace on the home grid, else the shown window (the pinned one as its app). */
export type PhonePill =
  | { readonly kind: "home"; readonly title: string }
  | {
      readonly kind: "window";
      readonly window: WindowRecord;
      readonly app: AppRecord | undefined;
      readonly title: string;
    };

/** The name the home grid's pill shows when the workspace has none. */
export const DEFAULT_WORKSPACE_NAME = "Workspace";

export function phonePillOf(state: DesktopState): PhonePill {
  const window = shownWindowOf(state);
  if (window === null) return { kind: "home", title: state.workspaceName || DEFAULT_WORKSPACE_NAME };
  const app = appByName(state, window.app);
  // The pinned window's own title is the page's (the chat on screen); the pill names the window it always is.
  const title = window.is_pinned ? (app?.display_name ?? window.app) : effectiveWindowTitle(state, window, app);
  return { kind: "window", window, app, title };
}
