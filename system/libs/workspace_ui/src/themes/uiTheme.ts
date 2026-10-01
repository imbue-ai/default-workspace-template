/// <reference types="vite/client" />
/**
 * The retro looks a desktop can wear (Desktop settings > Theme).
 *
 * `applyUiTheme` writes the theme onto `<html data-ui-theme="...">`, and each theme's stylesheet
 * (`themes/*.css`) is scoped to its own value, so the standard look -- or a theme name this build
 * does not know -- matches none of them. The names mirror `DesktopTheme` in the shell's
 * `primitives.py`; the two lists change together.
 */

export type UiTheme = "default" | "mac-classic" | "windows-2000";

export const UI_THEMES: readonly { readonly name: UiTheme; readonly label: string }[] = [
  { name: "default", label: "Standard" },
  { name: "mac-classic", label: "Classic Mac" },
  { name: "windows-2000", label: "Windows 2000" },
];

export const DEFAULT_UI_THEME: UiTheme = "default";

const UI_THEME_ATTRIBUTE = "data-ui-theme";

let current: UiTheme = DEFAULT_UI_THEME;

const listeners = new Set<(theme: UiTheme) => void>();

export function isUiTheme(value: unknown): value is UiTheme {
  return UI_THEMES.some((theme) => theme.name === value);
}

/** The theme the page wears now. */
export function currentUiTheme(): UiTheme {
  return current;
}

/** Wear the named theme, or the default look for a name this build does not know.
 *  Answers whether the look changed. */
export function applyUiTheme(root: Element, name: unknown): boolean {
  const theme = isUiTheme(name) ? name : DEFAULT_UI_THEME;
  if (theme === current) return false;
  current = theme;
  root.setAttribute(UI_THEME_ATTRIBUTE, theme);
  for (const listener of listeners) listener(theme);
  return true;
}

/** Hear every change of the page's theme (the shell passes it on to the app pages it frames). */
export function onUiThemeChanged(listener: (theme: UiTheme) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Wear whatever theme the parent page wears, for a page framed by a page of its own app on its own origin (the
 * chat root's chat pages), which the shell never messages directly. Answers whether there is such a parent.
 */
export function mirrorParentUiTheme(): boolean {
  if (window.parent === window) return false;
  let parentRoot: Element;
  try {
    parentRoot = window.parent.document.documentElement;
  } catch {
    return false;
  }
  const follow = (): void => void applyUiTheme(document.documentElement, parentRoot.getAttribute(UI_THEME_ATTRIBUTE));
  follow();
  new MutationObserver(follow).observe(parentRoot, { attributes: true, attributeFilter: [UI_THEME_ATTRIBUTE] });
  return true;
}

// Test-only: back to a freshly loaded page.
export function resetUiThemeForTests(): void {
  current = DEFAULT_UI_THEME;
  listeners.clear();
  document.documentElement.removeAttribute(UI_THEME_ATTRIBUTE);
}

// One folder per retro theme, keyed by the app's registry name; `app.png` is the icon for an app
// with none of its own.
const RETRO_ICON_URLS: Record<string, string> = import.meta.glob("./icons/*/*.png", {
  eager: true,
  query: "?url",
  import: "default",
});

/** `theme`'s pixel-art icon `name`, or null for the standard look and a name it has no icon for. */
export function retroIconUrlFor(theme: UiTheme, name: string): string | null {
  return RETRO_ICON_URLS[`./icons/${theme}/${name}.png`] ?? null;
}

/** The current theme's pixel-art icon for an app: its own when one was drawn for it, else the
 *  generic program icon. Null in the default theme. */
export function retroAppIconUrl(appName: string): string | null {
  if (current === DEFAULT_UI_THEME) return null;
  return RETRO_ICON_URLS[`./icons/${current}/${appName}.png`] ?? RETRO_ICON_URLS[`./icons/${current}/app.png`] ?? null;
}
