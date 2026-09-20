/**
 * The TypeScript mirrors of the shell's records (desktop-interface contracts.md sections 3 to
 * 5): what ``GET /api/desktops``, the placements routes, the client list, and the ``apps_updated``
 * push carry, spelled as the wire spells them (``snake_case``), and the parsers that read a wire
 * document into them. A document of the wrong shape is refused with ``WireShapeError`` rather
 * than read as an empty one: an empty desktop list would be believed.
 */

export type WindowState = "NORMAL" | "SNAPPED_LEFT" | "SNAPPED_RIGHT" | "MAXIMIZED";

export const WINDOW_STATES: readonly WindowState[] = ["NORMAL", "SNAPPED_LEFT", "SNAPPED_RIGHT", "MAXIMIZED"];

export type SharingMode = "shared" | "personal";

export type ShortcutMode = "focus" | "new";

export type IfPresent = "focus" | "new";

export type WallpaperKind = "bundled" | "file";

/** Whether a window's path and title are followed by every client, or kept by each client for itself. */
export type LocationScope = "linked" | "independent";

/** How a pinned entry is drawn: the plain icon-and-title entry, or the avatar the shell ships. */
export type PinStyle = "plain" | "avatar";

/** Where one client shows a pinned entry: in the taskbar, or floating above the windows. */
export type EntryMode = "bar" | "floating";

/** A window's rectangle in fractions of the backdrop, wholly inside the unit square. */
export interface Frame {
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

export interface GridCell {
  readonly column: number;
  readonly row: number;
}

export interface ShortcutTarget {
  readonly kind: "launch";
  readonly app: string;
  readonly launch: string;
}

export interface DesktopShortcut {
  readonly target: ShortcutTarget;
  readonly mode: ShortcutMode;
  readonly cell: GridCell;
}

export interface Wallpaper {
  readonly kind: WallpaperKind;
  readonly name: string;
}

export interface WindowRecord {
  readonly id: string;
  readonly app: string;
  readonly path: string;
  /** What the page last reported; "" means "show the app's display name". */
  readonly title: string;
  readonly opened_at: string;
  readonly is_settling: boolean;
  /** The app's pinned window on this desktop: permanent, never closed. */
  readonly is_pinned: boolean;
  readonly scope: LocationScope;
}

export interface Desktop {
  readonly id: string;
  readonly name: string;
  readonly color: string;
  readonly glyph: number;
  readonly sharing: SharingMode;
  readonly wallpaper: Wallpaper | null;
  readonly shortcuts: readonly DesktopShortcut[];
  readonly windows: readonly WindowRecord[];
}

export interface Placement {
  readonly window_id: string;
  readonly frame: Frame;
  readonly state: WindowState;
  readonly is_minimized: boolean;
}

/** One client's path and title for an independent window (pinned-taskbar-entries plan section 5.1). */
export interface StoredWindowPath {
  readonly path: string;
  readonly title: string;
}

/** One client's layout of one desktop: the placements, back to front, the stamp of the last save, and the
 *  client's stored paths for the desktop's independent windows, by window id. */
export interface Layout {
  readonly updated_at: string | null;
  readonly placements: readonly Placement[];
  readonly window_paths: Readonly<Record<string, StoredWindowPath>>;
}

export const EMPTY_LAYOUT: Layout = Object.freeze({
  updated_at: null,
  placements: Object.freeze([]),
  window_paths: Object.freeze({}),
});

export interface LaunchPath {
  readonly id: string;
  readonly label: string;
  readonly path: string;
  /** The names of the query parameters the shell may append. */
  readonly params: readonly string[];
}

export interface DefaultShortcut {
  /** The launch path id the shortcut runs. */
  readonly launch: string;
  readonly mode: ShortcutMode;
}

/** An app's pinned taskbar entry, as its manifest declares it (pinned-taskbar-entries plan section 3.1). */
export interface AppPin {
  /** The home path: where the pinned window opens. */
  readonly path: string;
  readonly style: PinStyle;
  readonly scope: LocationScope;
  readonly default_mode: EntryMode;
}

/** One registered app as the shell lists it (contracts.md section 5.5). */
export interface AppRecord {
  readonly name: string;
  readonly display_name: string;
  /** The app's own icon as SVG markup, or "" when it registered none. */
  readonly icon: string;
  /** The unguessable origin label its public origin uses ("" on a legacy row). */
  readonly label: string;
  readonly url: string;
  readonly internal: boolean;
  readonly program: string;
  readonly critical: boolean;
  /** The app's launch paths; the shell synthesizes ``open`` at ``/`` for an app declaring none. */
  readonly launch_paths: readonly LaunchPath[];
  readonly default_shortcut: DefaultShortcut | null;
  readonly launcher_rank: number | null;
  readonly pin: AppPin | null;
  readonly is_running: boolean;
}

/** Where a client keeps a floating entry: the top-left corner of its box, in fractions of the backdrop. */
export interface FloatingPosition {
  readonly x: number;
  readonly y: number;
}

/** How one client shows one pinned entry (pinned-taskbar-entries plan section 3.4). */
export interface EntryPresentation {
  readonly mode: EntryMode;
  readonly style: PinStyle;
  readonly position: FloatingPosition | null;
}

export interface ClientRecord {
  readonly id: string;
  readonly active_desktop: string | null;
  readonly last_seen: string;
  readonly is_connected: boolean;
  /** The client's presentation of each pinned entry, by app name. */
  readonly entries: Readonly<Record<string, EntryPresentation>>;
}

export interface WallpaperListing {
  readonly kind: WallpaperKind;
  readonly name: string;
  readonly url: string;
}

/** Raised when a wire document does not have the shape the contract gives it. */
export class WireShapeError extends Error {}

type Raw = Record<string, unknown>;

function asObject(value: unknown, what: string): Raw {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new WireShapeError(`${what} is not an object`);
  }
  return value as Raw;
}

function asArray(value: unknown, what: string): unknown[] {
  if (!Array.isArray(value)) throw new WireShapeError(`${what} is not an array`);
  return value;
}

function asString(value: unknown, what: string): string {
  if (typeof value !== "string") throw new WireShapeError(`${what} is not a string`);
  return value;
}

function asOptionalString(value: unknown, what: string): string | null {
  if (value === null || value === undefined) return null;
  return asString(value, what);
}

function asNumber(value: unknown, what: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new WireShapeError(`${what} is not a number`);
  return value;
}

function asBoolean(value: unknown, what: string): boolean {
  if (typeof value !== "boolean") throw new WireShapeError(`${what} is not a boolean`);
  return value;
}

function asOneOf<T extends string>(value: unknown, allowed: readonly T[], what: string): T {
  if (typeof value !== "string" || !(allowed as readonly string[]).includes(value)) {
    throw new WireShapeError(`${what} is not one of ${allowed.join(", ")}: ${JSON.stringify(value)}`);
  }
  return value as T;
}

export function parseFrame(raw: unknown): Frame {
  const record = asObject(raw, "frame");
  return {
    x: asNumber(record.x, "frame.x"),
    y: asNumber(record.y, "frame.y"),
    width: asNumber(record.width, "frame.width"),
    height: asNumber(record.height, "frame.height"),
  };
}

export function parseGridCell(raw: unknown): GridCell {
  const record = asObject(raw, "cell");
  return { column: asNumber(record.column, "cell.column"), row: asNumber(record.row, "cell.row") };
}

function parseShortcut(raw: unknown): DesktopShortcut {
  const record = asObject(raw, "shortcut");
  const target = asObject(record.target, "shortcut.target");
  return {
    target: {
      kind: "launch",
      app: asString(target.app, "shortcut.target.app"),
      launch: asString(target.launch, "shortcut.target.launch"),
    },
    mode: asOneOf(record.mode, ["focus", "new"], "shortcut.mode"),
    cell: parseGridCell(record.cell),
  };
}

export function parseWallpaper(raw: unknown): Wallpaper | null {
  if (raw === null || raw === undefined) return null;
  const record = asObject(raw, "wallpaper");
  return {
    kind: asOneOf(record.kind, ["bundled", "file"], "wallpaper.kind"),
    name: asString(record.name, "wallpaper.name"),
  };
}

export function parseWindow(raw: unknown): WindowRecord {
  const record = asObject(raw, "window");
  return {
    id: asString(record.id, "window.id"),
    app: asString(record.app, "window.app"),
    path: asString(record.path, "window.path"),
    title: asString(record.title, "window.title"),
    opened_at: asString(record.opened_at, "window.opened_at"),
    is_settling: asBoolean(record.is_settling, "window.is_settling"),
    // Both additive with defaults, so a V1 record reads unchanged.
    is_pinned: record.is_pinned === undefined ? false : asBoolean(record.is_pinned, "window.is_pinned"),
    scope: record.scope === undefined ? "linked" : asOneOf(record.scope, ["linked", "independent"], "window.scope"),
  };
}

export function parseDesktop(raw: unknown): Desktop {
  const record = asObject(raw, "desktop");
  return {
    id: asString(record.id, "desktop.id"),
    name: asString(record.name, "desktop.name"),
    color: asString(record.color, "desktop.color"),
    glyph: asNumber(record.glyph, "desktop.glyph"),
    sharing: asOneOf(record.sharing, ["shared", "personal"], "desktop.sharing"),
    wallpaper: parseWallpaper(record.wallpaper),
    shortcuts: asArray(record.shortcuts, "desktop.shortcuts").map(parseShortcut),
    windows: asArray(record.windows, "desktop.windows").map(parseWindow),
  };
}

export function parseDesktops(raw: unknown): Desktop[] {
  return asArray(raw, "desktops").map(parseDesktop);
}

export function parsePlacement(raw: unknown): Placement {
  const record = asObject(raw, "placement");
  return {
    window_id: asString(record.window_id, "placement.window_id"),
    frame: parseFrame(record.frame),
    state: asOneOf(record.state, WINDOW_STATES, "placement.state"),
    is_minimized: asBoolean(record.is_minimized, "placement.is_minimized"),
  };
}

function parseStoredWindowPath(raw: unknown): StoredWindowPath {
  const record = asObject(raw, "window path");
  return { path: asString(record.path, "window_path.path"), title: asString(record.title, "window_path.title") };
}

function parseWindowPaths(raw: unknown): Record<string, StoredWindowPath> {
  if (raw === undefined) return {};
  const record = asObject(raw, "layout.window_paths");
  return Object.fromEntries(
    Object.entries(record).map(([windowId, stored]) => [windowId, parseStoredWindowPath(stored)]),
  );
}

export function parseLayout(raw: unknown): Layout {
  const record = asObject(raw, "layout");
  return {
    updated_at: asOptionalString(record.updated_at, "layout.updated_at"),
    placements: asArray(record.placements, "layout.placements").map(parsePlacement),
    window_paths: parseWindowPaths(record.window_paths),
  };
}

function parseLaunchPath(raw: unknown): LaunchPath {
  const record = asObject(raw, "launch path");
  return {
    id: asString(record.id, "launch_path.id"),
    label: asString(record.label, "launch_path.label"),
    path: asString(record.path, "launch_path.path"),
    params: asArray(record.params ?? [], "launch_path.params").map((param) => asString(param, "launch_path.param")),
  };
}

function parseDefaultShortcut(raw: unknown): DefaultShortcut | null {
  if (raw === null || raw === undefined) return null;
  const record = asObject(raw, "default_shortcut");
  return {
    launch: asString(record.launch, "default_shortcut.launch"),
    mode: asOneOf(record.mode, ["focus", "new"], "default_shortcut.mode"),
  };
}

function parsePin(raw: unknown): AppPin | null {
  if (raw === null || raw === undefined) return null;
  const record = asObject(raw, "pin");
  return {
    path: asString(record.path, "pin.path"),
    style: asOneOf(record.style, ["plain", "avatar"], "pin.style"),
    scope: asOneOf(record.scope, ["linked", "independent"], "pin.scope"),
    default_mode: asOneOf(record.default_mode, ["bar", "floating"], "pin.default_mode"),
  };
}

export function parseAppRecord(raw: unknown): AppRecord {
  const record = asObject(raw, "app");
  const name = asString(record.name, "app.name");
  const rank = record.launcher_rank;
  return {
    name,
    display_name: typeof record.display_name === "string" && record.display_name !== "" ? record.display_name : name,
    icon: typeof record.icon === "string" ? record.icon : "",
    label: typeof record.label === "string" ? record.label : "",
    url: asString(record.url, "app.url"),
    internal: record.internal === true,
    program: typeof record.program === "string" ? record.program : "",
    critical: record.critical === true,
    launch_paths: asArray(record.launch_paths ?? [], "app.launch_paths").map(parseLaunchPath),
    default_shortcut: parseDefaultShortcut(record.default_shortcut),
    launcher_rank: typeof rank === "number" && Number.isFinite(rank) ? rank : null,
    pin: parsePin(record.pin),
    is_running: record.is_running === true,
  };
}

export function parseAppRecords(raw: unknown): AppRecord[] {
  return asArray(raw, "apps").map(parseAppRecord);
}

export function parseClientRecords(raw: unknown): ClientRecord[] {
  return asArray(raw, "clients").map(parseClientRecord);
}

function parseFloatingPosition(raw: unknown): FloatingPosition | null {
  if (raw === null || raw === undefined) return null;
  const record = asObject(raw, "position");
  return { x: asNumber(record.x, "position.x"), y: asNumber(record.y, "position.y") };
}

export function parseEntryPresentation(raw: unknown): EntryPresentation {
  const record = asObject(raw, "entry");
  return {
    mode: asOneOf(record.mode, ["bar", "floating"], "entry.mode"),
    style: asOneOf(record.style, ["plain", "avatar"], "entry.style"),
    position: parseFloatingPosition(record.position),
  };
}

/** The ``entries`` map of a client record or a ``client_entries_changed`` message; absent reads as none. */
export function parseEntries(raw: unknown): Record<string, EntryPresentation> {
  if (raw === undefined) return {};
  const record = asObject(raw, "entries");
  return Object.fromEntries(Object.entries(record).map(([app, entry]) => [app, parseEntryPresentation(entry)]));
}

export function parseClientRecord(raw: unknown): ClientRecord {
  const record = asObject(raw, "client");
  return {
    id: asString(record.id, "client.id"),
    active_desktop: asOptionalString(record.active_desktop, "client.active_desktop"),
    last_seen: asString(record.last_seen, "client.last_seen"),
    is_connected: record.is_connected === true,
    entries: parseEntries(record.entries),
  };
}

export function parseWallpaperListings(raw: unknown): WallpaperListing[] {
  return asArray(raw, "wallpapers").map(parseWallpaperListing);
}

export function parseWallpaperListing(raw: unknown): WallpaperListing {
  const record = asObject(raw, "wallpaper listing");
  return {
    kind: asOneOf(record.kind, ["bundled", "file"], "wallpaper.kind"),
    name: asString(record.name, "wallpaper.name"),
    url: asString(record.url, "wallpaper.url"),
  };
}

/** Whether two cells are the same cell. */
export function isSameCell(first: GridCell, second: GridCell): boolean {
  return first.column === second.column && first.row === second.row;
}

/** Whether two placements of the same window say the same thing (frame, state, minimized). */
export function isSamePlacement(first: Placement, second: Placement): boolean {
  return (
    first.window_id === second.window_id &&
    first.frame.x === second.frame.x &&
    first.frame.y === second.frame.y &&
    first.frame.width === second.frame.width &&
    first.frame.height === second.frame.height &&
    first.state === second.state &&
    first.is_minimized === second.is_minimized
  );
}

/** Whether two layouts hold the same stored paths and titles for the same windows. */
export function isSameWindowPaths(
  first: Readonly<Record<string, StoredWindowPath>>,
  second: Readonly<Record<string, StoredWindowPath>>,
): boolean {
  const firstIds = Object.keys(first);
  if (firstIds.length !== Object.keys(second).length) return false;
  return firstIds.every(
    (windowId) => second[windowId]?.path === first[windowId].path && second[windowId]?.title === first[windowId].title,
  );
}

/** The key a shortcut is unique under on a desktop, and the spelling of ``data-shortcut``. */
export function shortcutKey(app: string, launch: string): string {
  return `${app}:${launch}`;
}
