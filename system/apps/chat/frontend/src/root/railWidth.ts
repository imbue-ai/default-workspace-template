/**
 * The chat list's width, which the user sets by dragging its right edge. Kept per browser, in
 * storage, so it survives a reload and every root of this browser agrees. The desktop form of the
 * phone layout's drawer opens at the same width.
 */

export const DEFAULT_RAIL_WIDTH_PX = 180;
export const MIN_RAIL_WIDTH_PX = 100;
export const MAX_RAIL_WIDTH_PX = 480;

const STORAGE_KEY = "chat-root-rail-width";

let width = DEFAULT_RAIL_WIDTH_PX;

export function clampRailWidth(px: number): number {
  return Math.round(Math.min(MAX_RAIL_WIDTH_PX, Math.max(MIN_RAIL_WIDTH_PX, px)));
}

function load(): number {
  try {
    const stored = Number(window.localStorage.getItem(STORAGE_KEY));
    return stored > 0 ? clampRailWidth(stored) : DEFAULT_RAIL_WIDTH_PX;
  } catch {
    return DEFAULT_RAIL_WIDTH_PX;
  }
}

export function initRailWidth(): void {
  width = load();
  window.addEventListener("storage", (event: StorageEvent) => {
    if (event.key === STORAGE_KEY) width = load();
  });
}

export function railWidth(): number {
  return width;
}

/** Set the width for this page only, as a drag goes; ``saveRailWidth`` keeps it once the drag ends. */
export function setRailWidth(px: number): void {
  width = clampRailWidth(px);
}

export function saveRailWidth(): void {
  try {
    if (width === DEFAULT_RAIL_WIDTH_PX) window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, String(width));
  } catch {
    // A browser that refuses storage still gets the width for this page's lifetime.
  }
}
