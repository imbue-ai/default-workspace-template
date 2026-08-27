/**
 * The History pane: where a timeline lives, which apps have one, and what the
 * shell lets you do to the pane showing it.
 *
 * The Versioning app (registered service `versioning`) serves a per-app
 * timeline at `/app/<service-name>` -- keyed by the same service name every
 * other ref on the machine is. That page is reached from an app's own menus
 * ("History") rather than from a tab of its own: the timeline is always
 * *about* some app, so a standalone tab would have to open by asking which
 * one.
 *
 * The shell treats a pane of that service as a PRIMITIVE rather than as an app
 * the user installed. It is always called "History", always wears the clock
 * glyph, and carries only the two verbs about what the pane is showing
 * (Refresh, and putting it away) -- there is no sharing a history, no filing
 * one into a project, and above all no stopping or deleting one. The naming and
 * the glyph are `HISTORY_PANE_TITLE` and the `history` icon, applied wherever a
 * service is named or drawn; the verb set is `historyPaneMenuActions` below.
 *
 * The rules here are pure and take the app lists as arguments -- the same shape
 * models/appLiveness uses, and for the same reason: every menu surface (the
 * dock tab, the rail row, the rail's shortcut rows, the view switcher) and
 * their tests can read them without a dock and without restating AgentManager's
 * module state in every mock. They live here rather than in objectMenu.ts
 * because that module is deliberately about verb ORDER and knows nothing about
 * any particular service.
 */

import type { AppEntry } from "../models/AgentManager";
import type { ObjectMenuActions } from "./objectMenu";

/** The registered service that serves every app's timeline. */
export const VERSIONING_SERVICE_NAME = "versioning";

/**
 * The workspace shell's own timeline, spelled the way the VERSIONING APP
 * spells it -- with a hyphen.
 *
 * The shell is versioned like everything else under `system/apps`, and the
 * versioning app names apps after their folder with underscores turned into
 * hyphens (`_service_name_for_package_dir` in the versioning package's
 * history.py). So the folder is `system_interface`, the service registry (which
 * takes its name from `forward_port.py`) says `system_interface` too, and the
 * one name the timeline answers to is `system-interface` -- listed there as
 * "System", and browse-only, since reviving the shell safely wants the
 * update-system-interface machinery rather than a folder restore
 * (`UNRESTORABLE_APP_NAMES` in the versioning package's restore.py).
 *
 * Every other app is registered kebab-case (see the build-app skill) and so
 * spells the same either way; this is the lone exception, which is exactly why
 * it is written down here. It is also why the System's History is offered by
 * the shell's own workspace menu rather than by a pane of the shell: the shell
 * is not a tab-able app (see AllAppsPicker's HIDDEN_APP_NAMES), so there is no
 * pane of it whose menu could carry the row.
 */
export const SYSTEM_HISTORY_APP_NAME = "system-interface";

/** What every pane, tab and row of the versioning service reads. Not "the app
 *  called versioning" and not numbered per instance: the pane is one of the
 *  shell's primitives, and a second one is still the History. */
export const HISTORY_PANE_TITLE = "History";

/** The Versioning app's page for one app's timeline, as a `/`-rooted path --
 *  the same shape the location beacon stores (the timeline page beacons this
 *  very path), so a pane pointed here and a pane restored from a beacon spell
 *  the same address. */
export function appHistoryPath(serviceName: string): string {
  return `/app/${encodeURIComponent(serviceName)}`;
}

/** Whether a pane/row/tab of `serviceName` is the History primitive rather than
 *  an ordinary app. The one question every surface that names, draws, or builds
 *  a menu for a service asks before treating it as an app. */
export function isHistoryService(serviceName: string | null | undefined): boolean {
  return serviceName === VERSIONING_SERVICE_NAME;
}

/**
 * Whether one app's menus should offer History.
 *
 * Three things have to hold, and each rules out a different way the row would
 * be a lie -- a menu row that opens a 404 being worse than no row at all:
 *
 *  - The versioning service is registered. There is nothing to open otherwise,
 *    and a workspace can legitimately be running without it. This check
 *    deliberately does NOT read the versioning app's own `internal` flag: that
 *    flag hides an app from the LISTINGS a user browses ("All apps", the
 *    rail's shortcut rows), which is exactly what the versioning app wants --
 *    it is reached from here instead. Hidden from a list is not the same as
 *    unroutable. Its liveness is not read here either -- but note that the
 *    served list below is READ from it, so being able to answer once is what
 *    the rows really depend on: a service stopped before that first read
 *    withholds them until a later refresh gets through, while one stopped
 *    afterwards costs nothing, since a failed fetch leaves the last good answer
 *    standing (see models/VersionedApps). Once a row is drawn it is honest
 *    whatever the service is doing, because the click starts it on the way to
 *    the pane (see `openAppHistory`).
 *  - The app being asked about is one the versioning app actually SERVES.
 *    `versionedAppNames` is the versioning app's own `GET /api/apps` -- the
 *    list it answers `/app/<name>` for -- so this is the question itself rather
 *    than a proxy for it. The proxy it replaces ("registered, and not
 *    `internal`") was wrong in both directions: a registered port with no
 *    package of its own behind it (`si-preview` during a preview, mngr's
 *    `owner-exec`) got a row onto a 404, while the shell's own timeline, which
 *    is served under a name nothing registers, could have no row at all. `null`
 *    means the list has not been fetched yet (or could not be); no row until it
 *    is, because guessing is what this replaces.
 *  - The app is not the versioning app itself. A timeline of the timeline
 *    exists, but it is noise on the one tab already showing a timeline -- and
 *    that pane's menu offers no History at all (see `historyPaneMenuActions`).
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
 * The verb set a History pane gets, in place of an app pane's.
 *
 * A history is not an object the user made, so none of an app's verbs mean
 * anything on it: there is no service to share or stop behind it (the
 * versioning app is the shell's own machinery), no reason to file the pane into
 * a project, and above all nothing to delete -- deleting a history is not
 * something the workspace should offer at all, which is what this exists to
 * make true by construction rather than by each surface remembering to withhold
 * five verbs. What is left is the two verbs about what the pane is SHOWING:
 * reload it, and put it away.
 *
 * Built as an `ObjectMenuActions` rather than as a hand-rolled row list so both
 * surfaces still go through `objectMenuEntries` -- the ordering, the dividers,
 * and the "a menu must not end on a rule" rule stay settled in one place, and
 * every verb here is omitted by the null the interface already defines for it.
 * `hideTab` and `removeFromProject` are each one surface's own way of saying
 * "stop showing this here" (the dock tab hides, the rail unfiles), so each
 * caller passes its own and null for the other, exactly as it does for an app.
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
    // The pane's name is derived from what it IS -- always "History", never
    // numbered -- so there is nothing here for anyone to choose. Withheld by
    // the null rather than by an app's kind happening not to be renameable, so
    // the day `isRenameableKind` widens this pane does not quietly grow a verb.
    rename: null,
    hideTab: actions.hideTab,
    addToProjects: null,
    removeFromProject: actions.removeFromProject,
    stop: null,
    quit: null,
    serviceGroup: null,
  };
}
