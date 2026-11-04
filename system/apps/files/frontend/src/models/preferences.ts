/** How the user likes their folders shown, kept in the browser's localStorage so every Files window shares it. */

export type ViewMode = "icons" | "list" | "columns";
export type SortKey = "name" | "modified" | "size" | "kind";

export interface SortOrder {
  key: SortKey;
  descending: boolean;
}

export interface Preferences {
  viewMode: ViewMode;
  sort: SortOrder;
  showHidden: boolean;
  /** The sidebar's width in pixels, as the user dragged it. */
  sidebarWidth: number;
}

const VIEW_MODE_KEY = "files-view-mode";
const SORT_KEY = "files-sort";
// The key the File Viewer kept its "system files" toggle under, so the choice carries over.
const SHOW_HIDDEN_KEY = "minds-files-show-hidden";
const SIDEBAR_WIDTH_KEY = "files-sidebar-width";

export const MIN_SIDEBAR_WIDTH = 140;
export const MAX_SIDEBAR_WIDTH = 360;

const VIEW_MODES: readonly ViewMode[] = ["icons", "list", "columns"];
const SORT_KEYS: readonly SortKey[] = ["name", "modified", "size", "kind"];

export const DEFAULT_PREFERENCES: Preferences = {
  viewMode: "icons",
  sort: { key: "name", descending: false },
  showHidden: false,
  sidebarWidth: 208,
};

export function clampSidebarWidth(width: number): number {
  return Math.round(Math.min(MAX_SIDEBAR_WIDTH, Math.max(MIN_SIDEBAR_WIDTH, width)));
}

function read(storage: Storage, key: string): string | null {
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

function write(storage: Storage, key: string, value: string): void {
  try {
    storage.setItem(key, value);
  } catch {
    // A browser that refuses storage keeps the choice for this page only.
  }
}

export function loadPreferences(storage: Storage): Preferences {
  const viewMode = read(storage, VIEW_MODE_KEY);
  const [sortKey, direction] = (read(storage, SORT_KEY) ?? "").split(":");
  const storedWidth = Number.parseFloat(read(storage, SIDEBAR_WIDTH_KEY) ?? "");
  return {
    viewMode: VIEW_MODES.includes(viewMode as ViewMode) ? (viewMode as ViewMode) : DEFAULT_PREFERENCES.viewMode,
    sort: SORT_KEYS.includes(sortKey as SortKey)
      ? { key: sortKey as SortKey, descending: direction === "desc" }
      : DEFAULT_PREFERENCES.sort,
    showHidden: read(storage, SHOW_HIDDEN_KEY) === "true",
    sidebarWidth: Number.isFinite(storedWidth) ? clampSidebarWidth(storedWidth) : DEFAULT_PREFERENCES.sidebarWidth,
  };
}

export function savePreferences(storage: Storage, preferences: Preferences): void {
  write(storage, VIEW_MODE_KEY, preferences.viewMode);
  write(storage, SORT_KEY, `${preferences.sort.key}:${preferences.sort.descending ? "desc" : "asc"}`);
  write(storage, SHOW_HIDDEN_KEY, String(preferences.showHidden));
  write(storage, SIDEBAR_WIDTH_KEY, String(preferences.sidebarWidth));
}
