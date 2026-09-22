/**
 * Launch paths (desktop-interface plan section 3.1): the pure helpers over an app's declared
 * ways of starting something. The shell names no app: a seeded prompt goes to whichever app
 * declares a launch path with a ``message`` param, and the tiles lead with the apps that
 * declare a ``launcher_rank``.
 */

import type { AppRecord, LaunchPath } from "./records";

/** One "Open new" tile: an app and the launch path it runs. */
export interface LaunchTile {
  readonly app: AppRecord;
  readonly launchPath: LaunchPath;
}

// The query parameter a seeded prompt rides (contracts.md section 2: the manifest declares it on
// the launch path that takes a first message).
export const MESSAGE_PARAM = "message";
// The query parameter a draft rides: text for the page's composer, unsent, which the launch path at a pin's home
// path declares when the pinned window takes one (pinned-taskbar-entries plan section 4.7).
export const DRAFT_PARAM = "draft";

/** Every launch path of every openable app, as tiles, in registry and manifest order. */
export function launchTilesOf(apps: readonly AppRecord[]): LaunchTile[] {
  const tiles: LaunchTile[] = [];
  for (const app of apps) {
    if (app.internal) continue;
    for (const launchPath of app.launch_paths) tiles.push({ app, launchPath });
  }
  return tiles;
}

/** The tiles in display order: the apps that declare a ``launcher_rank``, lowest first (registry
 *  order breaks a tie), then every other app in registry order. */
export function orderLaunchTiles(tiles: readonly LaunchTile[]): LaunchTile[] {
  const ranked = tiles.filter((tile) => tile.app.launcher_rank !== null);
  const leading = [...ranked].sort((left, right) => (left.app.launcher_rank ?? 0) - (right.app.launcher_rank ?? 0));
  return [...leading, ...tiles.filter((tile) => tile.app.launcher_rank === null)];
}

/** Why a seeded prompt cannot be started: no app declares a launch path that takes a ``message``. */
export const NO_CHAT_APP_REASON = "No app on this machine can start a chat";

/** Where a seeded prompt goes: the first tile (in display order) whose launch path takes a ``message``. */
export function promptTargetOfTiles(tiles: readonly LaunchTile[]): LaunchTile | null {
  return orderLaunchTiles(tiles).find((tile) => tile.launchPath.params.includes(MESSAGE_PARAM)) ?? null;
}

/** The launch path of ``app`` with ``launchId``, or null when the app declares none by that id. */
export function launchPathOf(app: AppRecord, launchId: string): LaunchPath | null {
  return app.launch_paths.find((candidate) => candidate.id === launchId) ?? null;
}

/** The path the chat app serves a chat at: one page per chat, at ``/<chat-id>``. */
export function chatPath(chatId: string): string {
  return `/${chatId}`;
}

// The query parameter the chat app's root selects a chat with.
const CHAT_ROOT_SELECTION_PARAM = "chat";

/** The path of the chat app's root (the chat list beside the selected chat) with ``chatId`` selected. */
export function chatRootPath(chatId: string): string {
  return `/?${new URLSearchParams({ [CHAT_ROOT_SELECTION_PARAM]: chatId }).toString()}`;
}

/** Whether ``path`` is the chat app's root, whatever it has selected. */
export function isChatRootPath(path: string): boolean {
  return path.split("?")[0] === "/";
}

/** Whether ``path`` is showing the chat ``chatId``: the chat's own page, one of its subagent
 *  views (``/<chat-id>.<agent-id>.<session-id>``), or the root with the chat selected. */
export function isPathShowingChat(path: string, chatId: string): boolean {
  const [withoutQuery, query = ""] = path.split("?");
  if (withoutQuery === "/") return new URLSearchParams(query).get(CHAT_ROOT_SELECTION_PARAM) === chatId;
  return withoutQuery === chatPath(chatId) || withoutQuery.startsWith(`${chatPath(chatId)}.`);
}

/** The path a launch path opens at, with ``params`` as its query string (``/new?message=...``). */
export function launchPathWithParams(launchPath: LaunchPath, params: Readonly<Record<string, string>>): string {
  const query = new URLSearchParams();
  for (const [name, value] of Object.entries(params)) query.set(name, value);
  const encoded = query.toString();
  return encoded === "" ? launchPath.path : `${launchPath.path}?${encoded}`;
}

/** What a tile reads as when a search restates it as a row: "Open new terminal". */
export function launchRowLabel(tile: LaunchTile): string {
  return `Open new ${tile.app.display_name.toLowerCase()}`;
}
