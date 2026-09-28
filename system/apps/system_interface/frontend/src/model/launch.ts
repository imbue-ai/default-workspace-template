/**
 * Launch paths (desktop-interface plan section 3.1, launcher-and-getting-started plan section 3, the
 * post-launch-paths plan): the pure helpers over an app's declared ways of starting something. The
 * shell names no app: which launch paths take typed or drafted text is read from each one's
 * ``text_param`` or ``draft_param``, which app's window a focus row raises from its pin, and the
 * launcher leads with the apps that declare a ``launcher_rank``. The shell backend builds or asks for
 * every page a launch opens; nothing here builds a URL.
 */

import type { AppRecord, LaunchPath } from "./records";

/** One launch path of one app: what a launcher row runs. */
export interface AppLaunch {
  readonly app: AppRecord;
  readonly launchPath: LaunchPath;
}

// A window's path is at most this long (desktop contracts.md section 1, the shell's ``MAX_WINDOW_PATH_LENGTH``);
// a free-text row of a GET launch path whose path would be longer is disabled rather than refused by the shell.
export const MAX_WINDOW_PATH_LENGTH = 2048;

/** Why a free-text row stands down: the typed text, once encoded into the path, is over the path bound. */
export const TEXT_TOO_LONG_REASON = "Too long to send from here";

/** Why a text cannot be started anywhere: no app declares a launch path that takes typed text. */
export const NO_TEXT_APP_REASON = "No app on this machine can start a chat";
/** Why a page's ``shell:draft-text`` went nowhere. */
export const NO_DRAFT_APP_REASON = "No app on this machine can take a draft";

/** Every launch path of every openable app, in registry and manifest order. */
export function appLaunchesOf(apps: readonly AppRecord[]): AppLaunch[] {
  const launches: AppLaunch[] = [];
  for (const app of apps) {
    if (app.internal) continue;
    for (const launchPath of app.launch_paths) launches.push({ app, launchPath });
  }
  return launches;
}

/** The launches in launcher order: the apps that declare a ``launcher_rank``, lowest first (registry
 *  order breaks a tie), then every other app in registry order; manifest order within an app. */
export function orderAppLaunches(launches: readonly AppLaunch[]): AppLaunch[] {
  const ranked = launches.filter((launch) => launch.app.launcher_rank !== null);
  const leading = [...ranked].sort((left, right) => (left.app.launcher_rank ?? 0) - (right.app.launcher_rank ?? 0));
  return [...leading, ...launches.filter((launch) => launch.app.launcher_rank === null)];
}

/** The param a free-text row fills with the field's text: the launch path's ``text_param``, else its
 *  ``draft_param``; null for a launch path that takes no text. */
export function fillParamOf(launchPath: LaunchPath): string | null {
  return launchPath.text_param ?? launchPath.draft_param;
}

/**
 * What running a launch path comes to (plan sections 3.1 and 3.3), decided by the manifest alone and in this
 * order: ``text`` for a launch path with a ``text_param`` or a ``draft_param`` (a free-text row, whatever its
 * path); ``focus`` for a GET launch path at its app's pin path (the pinned window is raised, nothing opens);
 * ``new`` for every other.
 */
export type LaunchRowKind = "text" | "focus" | "new";

export function launchRowKindOf(app: AppRecord, launchPath: LaunchPath): LaunchRowKind {
  if (fillParamOf(launchPath) !== null) return "text";
  if (app.pin !== null && launchPath.method === "GET" && launchPath.path === app.pin.path) return "focus";
  return "new";
}

/** The free-text rows of the machine (plan section 3.1): every launch path with a ``text_param`` or a
 *  ``draft_param``, in launcher order; the first is the primary text action, the second the secondary. */
export function freeTextRowsOf(apps: readonly AppRecord[]): AppLaunch[] {
  return orderAppLaunches(appLaunchesOf(apps)).filter(
    (launch) => launchRowKindOf(launch.app, launch.launchPath) === "text",
  );
}

/** The draft rows of the machine (element-reference-menu plan section 6): the free-text rows whose launch path
 *  takes text to be drafted rather than sent, in launcher order. */
export function draftRowsOf(apps: readonly AppRecord[]): AppLaunch[] {
  return freeTextRowsOf(apps).filter((launch) => launch.launchPath.draft_param !== null);
}

/** The launch path of ``app`` with ``launchId``, or null when the app declares none by that id. */
export function launchPathOf(app: AppRecord, launchId: string): LaunchPath | null {
  return app.launch_paths.find((candidate) => candidate.id === launchId) ?? null;
}

/** The path the chat app serves a chat at: one page per chat, at ``/<chat-id>``. */
export function chatPath(chatId: string): string {
  return `/${chatId}`;
}

/** Whether ``path`` is showing the chat ``chatId``: the chat's own page, or one of its subagent
 *  views (``/<chat-id>.<agent-id>.<session-id>``). Any query string is ignored. */
export function isPathShowingChat(path: string, chatId: string): boolean {
  const withoutQuery = path.split("?")[0];
  return withoutQuery === chatPath(chatId) || withoutQuery.startsWith(`${chatPath(chatId)}.`);
}

/** The params a free-text row launches with: the text as its fill param; none for empty text. */
export function freeTextParams(launchPath: LaunchPath, text: string): Record<string, string> {
  const fillParam = fillParamOf(launchPath);
  if (fillParam === null || text === "") return {};
  return { [fillParam]: text };
}

/** The length of the page path a GET launch path opens at with ``params`` as its query (what the shell builds). */
function getLaunchPathLength(launchPath: LaunchPath, params: Readonly<Record<string, string>>): number {
  const query = new URLSearchParams();
  for (const [name, value] of Object.entries(launchPath.presets)) query.set(name, value);
  for (const [name, value] of Object.entries(params)) query.set(name, value);
  const encoded = query.toString();
  return encoded === "" ? launchPath.path.length : launchPath.path.length + 1 + encoded.length;
}

/**
 * Why a free-text row stands down for ``text``, or null while it can run (plan section 3.2): a launch path with
 * no text or draft param is disabled outright, and a GET launch path whose page path with the text would be over
 * the window-path bound is disabled too. The bound is on the encoded path, since encoding can triple a non-ASCII
 * text; a POST launch path carries the text in a body and has no such bound.
 */
export function textRowDisabledReason(launchPath: LaunchPath, text: string): string | null {
  if (fillParamOf(launchPath) === null) return NO_TEXT_APP_REASON;
  if (
    launchPath.method === "GET" &&
    getLaunchPathLength(launchPath, freeTextParams(launchPath, text)) > MAX_WINDOW_PATH_LENGTH
  ) {
    return TEXT_TOO_LONG_REASON;
  }
  return null;
}
