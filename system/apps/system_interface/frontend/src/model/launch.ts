/**
 * Launch paths (desktop-interface plan section 3.1, launcher-and-getting-started plan section 3):
 * the pure helpers over an app's declared ways of starting something. The shell names no app:
 * which launch paths take typed text is read from each one's ``text_param``, which app's window a
 * focus row raises from its pin, and the launcher leads with the apps that declare a
 * ``launcher_rank``.
 */

import type { AppRecord, LaunchPath } from "./records";

/** One launch path of one app: what a launcher row runs. */
export interface AppLaunch {
  readonly app: AppRecord;
  readonly launchPath: LaunchPath;
}

// The query parameter a draft rides: text for the page's composer, unsent, which the launch path at a pin's home
// path declares when the pinned window takes one (pinned-taskbar-entries plan section 4.7).
export const DRAFT_PARAM = "draft";

// A window's path is at most this long (desktop contracts.md section 1, the shell's ``MAX_WINDOW_PATH_LENGTH``);
// a free-text row whose path would be longer is disabled rather than refused by the shell.
export const MAX_WINDOW_PATH_LENGTH = 2048;

/** Why a free-text row stands down: the typed text, once encoded into the path, is over the path bound. */
export const TEXT_TOO_LONG_REASON = "Too long to send from here";

/** Why a text cannot be started anywhere: no app declares a launch path that takes typed text. */
export const NO_TEXT_APP_REASON = "No app on this machine can start a chat";

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

/**
 * What running a launch path comes to (plan sections 3.1 and 3.3), decided by the manifest alone and in this
 * order: ``text`` for a launch path with a ``text_param`` (a free-text row, whatever its path); ``focus`` for
 * one at its app's pin path (the pinned window is raised, nothing opens); ``new`` for every other.
 */
export type LaunchRowKind = "text" | "focus" | "new";

export function launchRowKindOf(app: AppRecord, launchPath: LaunchPath): LaunchRowKind {
  if (launchPath.text_param !== null) return "text";
  if (app.pin !== null && launchPath.path === app.pin.path) return "focus";
  return "new";
}

/** The free-text rows of the machine (plan section 3.1): every launch path with a ``text_param``, in launcher
 *  order; the first is the primary text action, the second the secondary. */
export function freeTextRowsOf(apps: readonly AppRecord[]): AppLaunch[] {
  return orderAppLaunches(appLaunchesOf(apps)).filter(
    (launch) => launchRowKindOf(launch.app, launch.launchPath) === "text",
  );
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

/** The path a launch path opens at, with ``params`` as its query string (``/new?message=...``). */
export function launchPathWithParams(launchPath: LaunchPath, params: Readonly<Record<string, string>>): string {
  const query = new URLSearchParams();
  for (const [name, value] of Object.entries(params)) query.set(name, value);
  const encoded = query.toString();
  return encoded === "" ? launchPath.path : `${launchPath.path}?${encoded}`;
}

/** Where a free-text row points its window: the path, or why it cannot (plan section 3.2). */
export type TextPath =
  { readonly kind: "path"; readonly path: string } | { readonly kind: "disabled"; readonly reason: string };

/**
 * The path a free-text launch path runs with ``text``: the launch path with the text as its text param, URL-encoded;
 * empty text runs the launch path with no param at all. The bound is on the encoded path, since encoding can
 * triple a non-ASCII text. A launch path with no text param is disabled outright.
 */
export function textPathOf(launchPath: LaunchPath, text: string): TextPath {
  if (launchPath.text_param === null) return { kind: "disabled", reason: NO_TEXT_APP_REASON };
  const path = text === "" ? launchPath.path : launchPathWithParams(launchPath, { [launchPath.text_param]: text });
  if (path.length > MAX_WINDOW_PATH_LENGTH) return { kind: "disabled", reason: TEXT_TOO_LONG_REASON };
  return { kind: "path", path };
}
