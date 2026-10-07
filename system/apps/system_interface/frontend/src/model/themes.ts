/**
 * The workspace's themes as the shell answers `GET /api/themes` and sends them in `themes_changed`
 * (docs/system/blueprint/workspace-themes/, section 5.2), and which one this client wears.
 */

import { STANDARD_THEME_ID, type ThemeRef } from "@imbue/workspace-ui/src/themes/themeClient";

export type ChromeSlot = "icon" | "title" | "refresh" | "menu" | "minimize" | "maximize" | "close";

export interface ThemeChrome {
  readonly title_align: "start" | "center";
  readonly leading: readonly ChromeSlot[];
  readonly trailing: readonly ChromeSlot[];
}

export type IconDerivation = "none" | "pixelate" | "quantize" | "monochrome";

export interface ThemeIcons {
  readonly format: "svg" | "png";
  readonly size: number;
  readonly rendering: "smooth" | "pixelated";
  readonly background: "transparent" | "opaque";
  readonly palette: readonly string[];
  readonly max_colors: number | null;
  readonly derive: IconDerivation;
  readonly fallback_url: string | null;
  /** Curated and generated icons, by app name. */
  readonly apps: Readonly<Record<string, string>>;
}

export interface ThemeRecord {
  readonly id: string;
  readonly name: string;
  readonly description: string;
  readonly base: string | null;
  readonly source: "builtin" | "workspace";
  readonly available: boolean;
  readonly problems: readonly string[];
  readonly revision: string;
  readonly chrome: ThemeChrome | null;
  readonly icons: ThemeIcons | null;
}

export interface ThemeCatalog {
  /** The workspace's default theme; null wears the standard one. */
  readonly default: string | null;
  readonly themes: readonly ThemeRecord[];
}

export const STANDARD_CHROME: ThemeChrome = {
  title_align: "start",
  leading: ["icon", "title", "refresh", "menu"],
  trailing: ["minimize", "maximize", "close"],
};

/** The standard look when the shell has not answered yet, or answers without a standard theme. */
export const STANDARD_THEME_RECORD: ThemeRecord = {
  id: STANDARD_THEME_ID,
  name: "Standard",
  description: "",
  base: null,
  source: "builtin",
  available: true,
  problems: [],
  revision: "",
  chrome: STANDARD_CHROME,
  icons: null,
};

export const EMPTY_THEME_CATALOG: ThemeCatalog = { default: null, themes: [] };

const CHROME_SLOTS: readonly ChromeSlot[] = ["icon", "title", "refresh", "menu", "minimize", "maximize", "close"];
const DERIVATIONS: readonly IconDerivation[] = ["none", "pixelate", "quantize", "monochrome"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function slotsOf(value: unknown): ChromeSlot[] | null {
  if (!Array.isArray(value)) return null;
  const slots = value.filter((slot): slot is ChromeSlot => CHROME_SLOTS.includes(slot as ChromeSlot));
  return slots.length === value.length ? slots : null;
}

function parseChrome(value: unknown): ThemeChrome | null {
  if (!isRecord(value)) return null;
  const leading = slotsOf(value.leading);
  const trailing = slotsOf(value.trailing);
  if (leading === null || trailing === null) return null;
  return { title_align: value.title_align === "center" ? "center" : "start", leading, trailing };
}

function parseIcons(value: unknown): ThemeIcons | null {
  if (!isRecord(value)) return null;
  const apps: Record<string, string> = {};
  if (isRecord(value.apps)) {
    for (const [name, url] of Object.entries(value.apps)) if (typeof url === "string") apps[name] = url;
  }
  const derive = DERIVATIONS.includes(value.derive as IconDerivation) ? (value.derive as IconDerivation) : "none";
  return {
    format: value.format === "svg" ? "svg" : "png",
    size: typeof value.size === "number" && value.size > 0 ? value.size : 32,
    rendering: value.rendering === "pixelated" ? "pixelated" : "smooth",
    background: value.background === "opaque" ? "opaque" : "transparent",
    palette: Array.isArray(value.palette)
      ? value.palette.filter((color): color is string => typeof color === "string")
      : [],
    max_colors: typeof value.max_colors === "number" ? value.max_colors : null,
    derive,
    fallback_url: typeof value.fallback_url === "string" ? value.fallback_url : null,
    apps,
  };
}

function parseTheme(value: unknown): ThemeRecord | null {
  if (!isRecord(value) || typeof value.id !== "string") return null;
  return {
    id: value.id,
    name: stringOr(value.name, value.id),
    description: stringOr(value.description, ""),
    base: typeof value.base === "string" ? value.base : null,
    source: value.source === "workspace" ? "workspace" : "builtin",
    available: value.available === true,
    problems: Array.isArray(value.problems)
      ? value.problems.filter((problem): problem is string => typeof problem === "string")
      : [],
    revision: stringOr(value.revision, ""),
    chrome: parseChrome(value.chrome),
    icons: parseIcons(value.icons),
  };
}

/** The catalog from the wire; anything malformed in it is left out rather than failing the whole read. */
export function parseThemeCatalog(value: unknown): ThemeCatalog {
  if (!isRecord(value)) return EMPTY_THEME_CATALOG;
  const themes = Array.isArray(value.themes)
    ? value.themes.map(parseTheme).filter((theme): theme is ThemeRecord => theme !== null)
    : [];
  return { default: typeof value.default === "string" ? value.default : null, themes };
}

function availableTheme(catalog: ThemeCatalog, id: string | null): ThemeRecord | null {
  if (id === null) return null;
  return catalog.themes.find((theme) => theme.id === id && theme.available) ?? null;
}

/**
 * The theme a desktop wears: its own choice, else the workspace's default, else the standard look; a choice that is
 * not available (deleted, or failing validation) is worn as the standard look.
 */
export function resolveDesktopTheme(catalog: ThemeCatalog, desktopTheme: string | null): ThemeRecord {
  const standard = availableTheme(catalog, STANDARD_THEME_ID) ?? STANDARD_THEME_RECORD;
  if (desktopTheme !== null) return availableTheme(catalog, desktopTheme) ?? standard;
  return availableTheme(catalog, catalog.default) ?? standard;
}

export function themeRefOf(theme: ThemeRecord): ThemeRef {
  return { id: theme.id, revision: theme.revision };
}

export function chromeOf(theme: ThemeRecord): ThemeChrome {
  return theme.chrome ?? STANDARD_CHROME;
}
