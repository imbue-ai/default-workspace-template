/**
 * The History pane: where a timeline lives, which apps have one, and what the
 * shell lets you do to the pane showing it.
 *
 * The Versioning app serves a per-app timeline at `/app/<service-name>`,
 * reached from an app's own menus ("History") rather than from a tab of its
 * own: the timeline is always *about* some app. The shell treats a pane of
 * that service as a PRIMITIVE, not an installed app -- always titled
 * "History", always the clock glyph, and carrying only Refresh and
 * put-it-away (never share, file, stop, or delete).
 *
 * The rules here are pure and take the app lists as arguments (like
 * models/appLiveness) so every menu surface and its tests can read them
 * without restating AgentManager's module state; objectMenu.ts stays about
 * verb ORDER and knows no particular service.
 */

import type { AppEntry } from "../models/AgentManager";
import type { ObjectMenuActions } from "./objectMenu";

/** The registered service that serves every app's timeline. */
export const VERSIONING_SERVICE_NAME = "versioning";

/**
 * The workspace shell's own timeline, spelled the way the VERSIONING APP
 * spells it. The versioning app names apps after their folder with underscores
 * turned to hyphens, so the registry's `system_interface` answers only as
 * `system-interface` -- the lone name that diverges, every other app being
 * registered kebab-case already. Offered from the workspace menu because the
 * shell is not a tab-able app, so no pane's menu could carry the row.
 */
export const SYSTEM_HISTORY_APP_NAME = "system-interface";

/** What every pane, tab and row of the versioning service reads -- never
 *  numbered per instance: a second pane is still the History. */
export const HISTORY_PANE_TITLE = "History";

/** The Versioning app's page for one app's timeline, as a `/`-rooted path --
 *  the same path the timeline page itself beacons, so a pane pointed here and
 *  one restored from a beacon spell the same address. */
export function appHistoryPath(serviceName: string): string {
  return `/app/${encodeURIComponent(serviceName)}`;
}

/** Whether a pane/row/tab of `serviceName` is the History primitive rather
 *  than an ordinary app. */
export function isHistoryService(serviceName: string | null | undefined): boolean {
  return serviceName === VERSIONING_SERVICE_NAME;
}

/**
 * Whether one app's menus should offer History -- a row that opens a 404 being
 * worse than no row. Three things must hold:
 *
 *  - The versioning service is registered. Its `internal` flag is deliberately
 *    NOT read (that hides it from listings, not from routing), nor its
 *    liveness (the click starts it on the way to the pane).
 *  - The app is one the versioning app actually SERVES. The registry proxy
 *    this replaces was wrong both ways: a packageless port (`si-preview`,
 *    `owner-exec`) got a row onto a 404, while the shell's timeline is served
 *    under a name nothing registers. `null` means not fetched yet: no row,
 *    because guessing is what this replaces.
 *  - The app is not the versioning app itself -- a timeline of the timeline
 *    is noise on the one tab already showing one.
 */
export function isAppHistoryOffered(
  apps: readonly AppEntry[],
  versionedAppNames: ReadonlySet<string> | null,
  serviceName: string,
): boolean {
  if (isHistoryService(serviceName)) return false;
  if (versionedAppNames === null || !versionedAppNames.has(serviceName)) return false;
  return apps.some((app) => app.name === VERSIONING_SERVICE_NAME);
}

/**
 * The verb set a History pane gets, in place of an app pane's: only the two
 * verbs about what the pane is SHOWING (reload it, put it away). A history is
 * not an object the user made -- above all, deleting one must never be offered
 * -- and this makes that true by construction rather than by each surface
 * withholding five verbs. Built as an `ObjectMenuActions` so both surfaces
 * still go through `objectMenuEntries` for ordering and divider rules;
 * `hideTab`/`removeFromProject` are each one surface's own "stop showing this
 * here", so each caller passes its own and null for the other.
 */
export function historyPaneMenuActions(actions: {
  refresh: () => void;
  hideTab: (() => void) | null;
  removeFromProject: (() => void) | null;
}): ObjectMenuActions {
  return {
    refresh: actions.refresh,
    // A timeline of the timeline is noise on the tab already showing one.
    history: null,
    share: null,
    // The name is derived, never chosen -- withheld by the null so the day
    // `isRenameableKind` widens this pane does not quietly grow a verb.
    rename: null,
    hideTab: actions.hideTab,
    addToProjects: null,
    removeFromProject: actions.removeFromProject,
    stop: null,
    quit: null,
    serviceGroup: null,
  };
}
