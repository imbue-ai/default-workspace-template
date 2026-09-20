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

/** One client's layout of one desktop: the placements, back to front, and the stamp of the last save. */
export interface Layout {
  readonly updated_at: string | null;
  readonly placements: readonly Placement[];
}

export const EMPTY_LAYOUT: Layout = Object.freeze({ updated_at: null, placements: Object.freeze([]) });

export interface LaunchPath {
  readonly id: string;
  readonly label: string;
  readonly path: string;
  /** The names of the query parameters the shell may append. */
  readonly params: readonly string[];
}

export interface DefaultShortcut {
  /** The launch path id the shortcut runs; null on a manifest written for the tabbed shell alone. */
  readonly launch: string | null;
  readonly mode: ShortcutMode;
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
  readonly is_running: boolean;
}

export interface ClientRecord {
  readonly id: string;
  readonly active_desktop: string | null;
  readonly last_seen: string;
  readonly is_connected: boolean;
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

export function parseLayout(raw: unknown): Layout {
  const record = asObject(raw, "layout");
  return {
    updated_at: asOptionalString(record.updated_at, "layout.updated_at"),
    placements: asArray(record.placements, "layout.placements").map(parsePlacement),
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
    launch: asOptionalString(record.launch, "default_shortcut.launch"),
    mode: asOneOf(record.mode, ["focus", "new"], "default_shortcut.mode"),
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
    is_running: record.is_running === true,
  };
}

export function parseAppRecords(raw: unknown): AppRecord[] {
  return asArray(raw, "apps").map(parseAppRecord);
}

export function parseClientRecord(raw: unknown): ClientRecord {
  const record = asObject(raw, "client");
  return {
    id: asString(record.id, "client.id"),
    active_desktop: asOptionalString(record.active_desktop, "client.active_desktop"),
    last_seen: asString(record.last_seen, "client.last_seen"),
    is_connected: record.is_connected === true,
  };
}

export function parseWallpaperListing(raw: unknown): WallpaperListing {
  const record = asObject(raw, "wallpaper listing");
  return {
    kind: asOneOf(record.kind, ["bundled", "file"], "wallpaper.kind"),
    name: asString(record.name, "wallpaper.name"),
    url: asString(record.url, "wallpaper.url"),
  };
}

/** Whether two frames are the same rectangle. */
export function isSameFrame(first: Frame, second: Frame): boolean {
  return (
    first.x === second.x && first.y === second.y && first.width === second.width && first.height === second.height
  );
}

/** Whether two cells are the same cell. */
export function isSameCell(first: GridCell, second: GridCell): boolean {
  return first.column === second.column && first.row === second.row;
}

/** The key a shortcut is unique under on a desktop, and the spelling of ``data-shortcut``. */
export function shortcutKey(app: string, launch: string): string {
  return `${app}:${launch}`;
}
