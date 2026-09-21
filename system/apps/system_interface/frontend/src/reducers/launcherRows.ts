/**
 * The launcher's rows (launcher-and-getting-started plan section 3.5) as pure functions over the
 * desktop's state and the field's query: the launch-path rows (every launch path of every openable
 * app, less the free-text ones), the window rows (every window of every desktop, only while
 * typing), and the free-text rows (always, the first two bound to Enter and Ctrl+Enter), each
 * section kept in its order and narrowed by the query; and the highlight rule, with the arrow
 * keys' step through the enabled rows.
 */

import type { AppRecord, LaunchPath, WindowRecord } from "../model/records";
import { appLaunchesOf, freeTextRowsOf, launchRowKindOf, orderAppLaunches, textPathOf } from "../model/launch";
import { matchesQuery } from "@imbue/workspace-ui/src/search";
import { activePlacements, appByName, effectiveWindowTitle, isWindowMinimized, openableApps } from "./desktopState";
import type { DesktopState } from "./desktopState";

/** A row that runs one launch path of one app; the store decides focus or new from the manifest when it runs
 *  (plan section 3.3). */
export interface LaunchRow {
  readonly kind: "launch";
  /** The ``data-launcher-row`` spelling, unique across the menu. */
  readonly key: string;
  readonly app: AppRecord;
  readonly launchPath: LaunchPath;
  readonly label: string;
  /** The app's display name, when the label does not already say it. */
  readonly caption: string | null;
}

/** A row that shows an existing window, on this or another desktop. */
export interface WindowRow {
  readonly kind: "window";
  readonly key: string;
  readonly window: WindowRecord;
  readonly desktopId: string;
  readonly desktopName: string;
  readonly app: AppRecord | undefined;
  readonly title: string;
  readonly isMinimized: boolean;
  readonly isOnActiveDesktop: boolean;
}

/** The first two free-text rows: Enter's fallthrough, and Ctrl+Enter's target. */
export type TextAction = "primary" | "secondary";

/** A row that runs a launch path with the field's text as its text param (plan section 3.2). */
export interface TextRow {
  readonly kind: "text";
  readonly key: string;
  readonly app: AppRecord;
  readonly launchPath: LaunchPath;
  readonly textAction: TextAction | null;
  readonly label: string;
  /** The trimmed text the row would send. */
  readonly text: string;
  /** Why the row stands down (over the path bound, or nothing to send), or null while it can run. */
  readonly disabledReason: string | null;
}

export type LauncherRow = LaunchRow | WindowRow | TextRow;

/** The menu's rows by section, and flat in the order the menu draws them. */
export interface LauncherMenuRows {
  readonly launchRows: readonly LaunchRow[];
  readonly windowRows: readonly WindowRow[];
  readonly textRows: readonly TextRow[];
  readonly rows: readonly LauncherRow[];
  /** Whether a query was typed and no launch-path or window row survived it. */
  readonly isNoMatch: boolean;
}

/** Why the secondary text action stands down with nothing typed: it would send an empty message. */
export const NOTHING_TO_SEND_REASON = "Type something to send";

/** The ``data-launcher-row`` spelling of a launch-path or free-text row. */
export function launchRowKey(kind: "launch" | "text", app: string, launch: string): string {
  return `${kind}:${app}:${launch}`;
}

/** The ``data-launcher-row`` spelling of a window row. */
export function windowRowKey(windowId: string): string {
  return `window:${windowId}`;
}

function launchRowsOf(apps: readonly AppRecord[], query: string): LaunchRow[] {
  const rows: LaunchRow[] = [];
  for (const { app, launchPath } of orderAppLaunches(appLaunchesOf(apps))) {
    // A free-text launch path is listed once, at the foot, never as a launch-path row.
    if (launchRowKindOf(app, launchPath) === "text") continue;
    if (!matchesQuery(query, launchPath.label, app.display_name, app.name)) continue;
    rows.push({
      kind: "launch",
      key: launchRowKey("launch", app.name, launchPath.id),
      app,
      launchPath,
      label: launchPath.label,
      caption: launchPath.label === app.display_name ? null : app.display_name,
    });
  }
  return rows;
}

/** Every window of every desktop, the active desktop's first and in opening order, narrowed by the query. */
function windowRowsOf(state: DesktopState, query: string): WindowRow[] {
  const placements = activePlacements(state);
  const ordered = [...state.desktops].sort(
    (left, right) => Number(right.id === state.activeDesktopId) - Number(left.id === state.activeDesktopId),
  );
  const rows: WindowRow[] = [];
  for (const desktop of ordered) {
    const isOnActiveDesktop = desktop.id === state.activeDesktopId;
    for (const window of desktop.windows) {
      const app = appByName(state, window.app);
      const title = effectiveWindowTitle(state, window, app);
      if (!matchesQuery(query, title, desktop.name, app?.display_name ?? "", window.app)) continue;
      rows.push({
        kind: "window",
        key: windowRowKey(window.id),
        window,
        desktopId: desktop.id,
        desktopName: desktop.name,
        app,
        title,
        isMinimized: isOnActiveDesktop ? isWindowMinimized(placements, window.id) : false,
        isOnActiveDesktop,
      });
    }
  }
  return rows;
}

function textRowsOf(apps: readonly AppRecord[], text: string): TextRow[] {
  return freeTextRowsOf(apps).map(({ app, launchPath }, index) => {
    const textAction: TextAction | null = index === 0 ? "primary" : index === 1 ? "secondary" : null;
    const target = textPathOf(launchPath, text);
    // Empty text runs the primary action with no text param at all; every other row needs something to send.
    const disabledReason =
      target.kind === "disabled" ? target.reason : text === "" && index > 0 ? NOTHING_TO_SEND_REASON : null;
    return {
      kind: "text",
      key: launchRowKey("text", app.name, launchPath.id),
      app,
      launchPath,
      textAction,
      label: launchPath.label,
      text,
      disabledReason,
    };
  });
}

/** The menu for ``query`` (plan section 3.5): window rows appear only while typing; free-text rows always. */
export function launcherRowsOf(state: DesktopState, query: string): LauncherMenuRows {
  const text = query.trim();
  const apps = openableApps(state);
  const launchRows = launchRowsOf(apps, text);
  const windowRows = text === "" ? [] : windowRowsOf(state, text);
  const textRows = textRowsOf(apps, text);
  return {
    launchRows,
    windowRows,
    textRows,
    rows: [...launchRows, ...windowRows, ...textRows],
    isNoMatch: text !== "" && launchRows.length === 0 && windowRows.length === 0,
  };
}

export function isRowEnabled(row: LauncherRow): boolean {
  return row.kind !== "text" || row.disabledReason === null;
}

/** The row Enter runs when nothing moved the highlight: the first launch-path or window row, else the primary
 *  text action (the first enabled text row); -1 when the menu has no enabled row. */
export function defaultHighlightIndex(rows: readonly LauncherRow[]): number {
  const firstMatch = rows.findIndex((row) => row.kind !== "text");
  if (firstMatch >= 0) return firstMatch;
  return rows.findIndex((row) => row.kind === "text" && isRowEnabled(row));
}

/** The highlight after an arrow key: ``delta`` enabled rows along from ``index`` (the default when the index names
 *  nothing enabled), wrapping at both ends; -1 when nothing is enabled. */
export function moveHighlight(rows: readonly LauncherRow[], index: number, delta: number): number {
  const enabled = rows.map((row, position) => (isRowEnabled(row) ? position : -1)).filter((position) => position >= 0);
  if (enabled.length === 0) return -1;
  const from = enabled.indexOf(enabled.includes(index) ? index : defaultHighlightIndex(rows));
  const start = from < 0 ? 0 : from;
  return enabled[(((start + delta) % enabled.length) + enabled.length) % enabled.length];
}

/** The row Ctrl+Enter runs whatever is highlighted, when it is enabled. */
export function secondaryTextRow(rows: readonly LauncherRow[]): TextRow | null {
  const row = rows.find((candidate) => candidate.kind === "text" && candidate.textAction === "secondary");
  return row !== undefined && row.kind === "text" && isRowEnabled(row) ? row : null;
}
