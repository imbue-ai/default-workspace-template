/**
 * What a shortcut, a launcher tile, or a desktop's menu comes to (desktop-interface plan sections
 * 3.6, 4.8, 4.9): a focus-mode shortcut raises the app's most recently focused window in this
 * client's layout and opens at the launch path only when there is none; a new desktop is minted
 * ``Desktop <n>`` with the next unused glyph; a shortcut added from a tile lands at the first free
 * cell in reading order over the current grid.
 */

import { drawnCells, firstFreeCellInReadingOrder, placeShortcuts } from "../geometry/grid";
import type { GridDimensions } from "../geometry/grid";
import { mostRecentlyFocusedWindowOfApp } from "../geometry/stack";
import { launchPathOf } from "../model/launch";
import type { Desktop, GridCell, ShortcutMode } from "../model/records";
import { activeDesktop, appByName } from "./desktopState";
import type { DesktopState } from "./desktopState";

export type ShortcutRun =
  | { readonly kind: "raise"; readonly windowId: string }
  | { readonly kind: "open"; readonly app: string; readonly path: string; readonly launch: string }
  | { readonly kind: "unavailable"; readonly reason: string };

/** What running the launch path of ``app`` in ``mode`` on the active desktop comes to. */
export function resolveLaunchRun(
  state: DesktopState,
  appName: string,
  launch: string,
  mode: ShortcutMode,
): ShortcutRun {
  const app = appByName(state, appName);
  if (app === undefined) return { kind: "unavailable", reason: `${appName} is not registered` };
  const launchPath = launchPathOf(app, launch);
  if (launchPath === null) return { kind: "unavailable", reason: `${app.display_name} has no launch path ${launch}` };
  const desktop = activeDesktop(state);
  if (desktop === null) return { kind: "unavailable", reason: "there is no desktop to open on" };
  if (mode === "focus") {
    const recent = mostRecentlyFocusedWindowOfApp(state.layout, desktop, app.name);
    if (recent !== null) return { kind: "raise", windowId: recent.id };
  }
  return { kind: "open", app: app.name, path: launchPath.path, launch: launchPath.id };
}

/** The name a fresh desktop gets: the first "Desktop N" nobody is using, by name or by id. */
export function nextDesktopName(desktops: readonly Pick<Desktop, "name" | "id">[]): string {
  const takenNames = new Set(desktops.map((desktop) => desktop.name.trim().toLowerCase()));
  const takenIds = new Set(desktops.map((desktop) => desktop.id));
  let index = 1;
  while (takenNames.has(`desktop ${index}`) || takenIds.has(`desktop-${index}`)) index += 1;
  return `Desktop ${index}`;
}

/** The glyph a fresh desktop gets: the first of the ``glyphCount`` glyphs nobody uses, then repeating. */
export function nextGlyphIndex(usedGlyphs: readonly number[], glyphCount: number): number {
  const used = new Set(usedGlyphs);
  for (let index = 0; index < glyphCount; index += 1) {
    if (!used.has(index)) return index;
  }
  return usedGlyphs.length % glyphCount;
}

/** Where a shortcut added with no cell goes: the first cell in reading order over the current grid
 *  that no shortcut draws in. */
export function cellForAddedShortcut(desktop: Desktop, dimensions: GridDimensions): GridCell {
  return firstFreeCellInReadingOrder(drawnCells(placeShortcuts(desktop.shortcuts, dimensions)), dimensions.columns);
}
