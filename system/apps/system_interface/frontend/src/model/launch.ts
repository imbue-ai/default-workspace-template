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

/** Where a seeded prompt goes: the first tile (in display order) whose launch path takes a ``message``. */
export function promptTargetOfTiles(tiles: readonly LaunchTile[]): LaunchTile | null {
  return orderLaunchTiles(tiles).find((tile) => tile.launchPath.params.includes(MESSAGE_PARAM)) ?? null;
}

/** The launch path of ``app`` with ``launchId``, or null when the app declares none by that id. */
export function launchPathOf(app: AppRecord, launchId: string): LaunchPath | null {
  return app.launch_paths.find((candidate) => candidate.id === launchId) ?? null;
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
