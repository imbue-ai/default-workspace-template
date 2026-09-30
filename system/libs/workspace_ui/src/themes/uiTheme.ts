/// <reference types="vite/client" />
/**
 * The theme the Imbue Studio chrome wears, mirrored onto this page so the workspace can match it.
 *
 * The chrome sends its theme with the embed contract's `minds:ui-theme` message (see `embed.ts`);
 * `applyUiTheme` writes it onto `<html data-ui-theme="...">`, and each theme's stylesheet
 * (`themes/*.css`) is scoped to its own value, so a page with the default look -- no chrome, an
 * older chrome, or a theme name this build does not know -- matches none of them.
 *
 * The themes, their vendored libraries (`vendor/`) and their pixel-art icons (`icons/`) come from
 * Imbue Studio (`apps/minds/frontend/src/themes/` in mngr), where the scripts that generate them
 * live; the two repos share no package, so they are copied by hand and updated together.
 */

export type UiTheme = "default" | "mac-classic" | "windows-2000";

const UI_THEMES: readonly UiTheme[] = ["default", "mac-classic", "windows-2000"];

export const DEFAULT_UI_THEME: UiTheme = "default";

const UI_THEME_ATTRIBUTE = "data-ui-theme";

let current: UiTheme = DEFAULT_UI_THEME;

export function isUiTheme(value: unknown): value is UiTheme {
  return typeof value === "string" && (UI_THEMES as readonly string[]).includes(value);
}

/** The theme the page wears now. */
export function currentUiTheme(): UiTheme {
  return current;
}

/** Wear the theme the chrome named, or the default look for a name this build does not know.
 *  Answers whether the look changed. */
export function applyUiTheme(root: Element, name: unknown): boolean {
  const theme = isUiTheme(name) ? name : DEFAULT_UI_THEME;
  if (theme === current) return false;
  current = theme;
  root.setAttribute(UI_THEME_ATTRIBUTE, theme);
  return true;
}

// Test-only: back to a freshly loaded page.
export function resetUiThemeForTests(): void {
  current = DEFAULT_UI_THEME;
}

// One folder per retro theme, keyed by the app's registry name; `app.png` is the icon for an app
// with none of its own.
const RETRO_ICON_URLS: Record<string, string> = import.meta.glob("./icons/*/*.png", {
  eager: true,
  query: "?url",
  import: "default",
});

/** The current theme's pixel-art icon for an app: its own when one was drawn for it, else the
 *  generic program icon. Null in the default theme. */
export function retroAppIconUrl(appName: string): string | null {
  if (current === DEFAULT_UI_THEME) return null;
  return RETRO_ICON_URLS[`./icons/${current}/${appName}.png`] ?? RETRO_ICON_URLS[`./icons/${current}/app.png`] ?? null;
}
