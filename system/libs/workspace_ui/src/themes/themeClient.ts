/**
 * A page's side of workspace themes (docs/system/blueprint/workspace-themes/, section 5.3).
 *
 * A theme is never compiled into a page: the page wears one by loading its bundle,
 * `/_static/themes/<id>/theme.css`, from its own origin, as the last stylesheet in `<head>`, so the theme's
 * unlayered rules beat every utility and, at equal specificity, the page's own rules. `standard` has no bundle:
 * a page with none loaded wears the standard look its own build carries.
 *
 * The page remembers the last theme in its origin's local storage, and `themeBoot()` (the Vite plugin in
 * `themeBoot.ts`) wears it before the first paint, so a page opens in its theme rather than flashing the
 * standard look first; the shell's `shell:theme` message then says which theme to wear.
 */

import {
  STANDARD_THEME_ID,
  THEME_ATTRIBUTE,
  THEME_ID_PATTERN,
  THEME_LINK_ATTRIBUTE,
  THEME_REVISION_ATTRIBUTE,
  THEME_STATIC_PREFIX,
  THEME_STORAGE_KEY,
} from "./themeBoot";

export {
  STANDARD_THEME_ID,
  THEME_ATTRIBUTE,
  THEME_LINK_ATTRIBUTE,
  THEME_REVISION_ATTRIBUTE,
  THEME_STATIC_PREFIX,
  THEME_STORAGE_KEY,
};

export interface ThemeRef {
  readonly id: string;
  readonly revision: string;
}

export const STANDARD_THEME: ThemeRef = { id: STANDARD_THEME_ID, revision: "" };

let current: ThemeRef = STANDARD_THEME;
/** Whether the current theme is remembered for the origin's next load, rather than only shown (a preview). */
let isCurrentRemembered = true;
/** Whether the theme the boot script wore has been taken as the current one yet. */
let hasAdoptedBootedTheme = false;
const listeners = new Set<ThemeListener>();

/** Hears the theme the page wears, and whether it is remembered (false while it is only shown, as a preview). */
export type ThemeListener = (theme: ThemeRef, isRemembered: boolean) => void;

export function isThemeRef(value: unknown): value is ThemeRef {
  if (typeof value !== "object" || value === null) return false;
  const { id, revision } = value as { id?: unknown; revision?: unknown };
  return typeof id === "string" && THEME_ID_PATTERN.test(id) && typeof revision === "string";
}

/** The URL of a theme's bundle on this page's origin; none for the standard look. */
export function themeBundleUrl(theme: ThemeRef): string | null {
  if (theme.id === STANDARD_THEME_ID) return null;
  const query = theme.revision === "" ? "" : `?v=${encodeURIComponent(theme.revision)}`;
  return `${THEME_STATIC_PREFIX}${encodeURIComponent(theme.id)}/theme.css${query}`;
}

/** The theme the page wears now (what the boot script put on, until the page wears another). */
export function currentTheme(): ThemeRef {
  adoptBootedThemeOnce(document);
  return current;
}

/** Whether the theme the page wears now is remembered for the origin's next load, rather than only shown. */
export function isCurrentThemeRemembered(): boolean {
  return isCurrentRemembered;
}

function readRemembered(): ThemeRef | null {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === null) return null;
    const parsed: unknown = JSON.parse(stored);
    return isThemeRef(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function remember(theme: ThemeRef): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify(theme));
  } catch {
    // Storage refused (a private window, a sandboxed frame): the page still wears the theme, and only the next
    // load starts in the standard look until the shell says otherwise.
  }
}

function themeLinks(doc: Document): HTMLLinkElement[] {
  return Array.from(doc.head.querySelectorAll<HTMLLinkElement>(`link[${THEME_LINK_ATTRIBUTE}]`));
}

function adoptBootedThemeOnce(doc: Document): void {
  if (hasAdoptedBootedTheme) return;
  hasAdoptedBootedTheme = true;
  adoptBootedTheme(doc);
}

/** The theme the page started in: what the boot script put on `<html>`, or the standard look. */
function adoptBootedTheme(doc: Document): void {
  const id = doc.documentElement.getAttribute(THEME_ATTRIBUTE);
  const revision = doc.documentElement.getAttribute(THEME_REVISION_ATTRIBUTE) ?? "";
  const booted = { id: id ?? STANDARD_THEME_ID, revision };
  current = isThemeRef(booted) ? booted : STANDARD_THEME;
}

export interface WearThemeOptions {
  /** Hold the page's first frame until the stylesheet has loaded: for a theme put on while `<head>` is read. */
  readonly isRenderBlocking?: boolean;
  /** Remember the theme for this origin's next load (the default); false for a page that only shows a theme, like
   *  the theme gallery, which shares the shell's origin. */
  readonly isRemembered?: boolean;
}

/**
 * Wear a theme: load its bundle as the page's last stylesheet, mark `<html>`, and remember it. The returned
 * promise settles once the page shows the theme (its stylesheet has loaded, or failed and was dropped, which
 * leaves the standard look), which is when a reader of the theme's tokens can read them.
 */
export function wearTheme(theme: ThemeRef, doc: Document = document, options: WearThemeOptions = {}): Promise<void> {
  adoptBootedThemeOnce(doc);
  const isRemembered = options.isRemembered !== false;
  const isSame = theme.id === current.id && theme.revision === current.revision;
  const existing = themeLinks(doc);
  const url = themeBundleUrl(theme);
  if (isSame && (url === null) === (existing.length === 0)) {
    // The theme stays; only whether it is remembered may change (a previewed theme saved as it is).
    if (isRemembered !== isCurrentRemembered) {
      isCurrentRemembered = isRemembered;
      if (isRemembered) remember(theme);
      for (const listener of listeners) listener(theme, isRemembered);
    }
    return Promise.resolve();
  }
  current = theme;
  isCurrentRemembered = isRemembered;
  const root = doc.documentElement;
  root.setAttribute(THEME_ATTRIBUTE, theme.id);
  root.setAttribute(THEME_REVISION_ATTRIBUTE, theme.revision);
  if (isRemembered) remember(theme);
  for (const listener of listeners) listener(theme, isRemembered);
  if (url === null) {
    for (const link of existing) link.remove();
    return Promise.resolve();
  }
  // The new stylesheet goes in beside the old one, which stays until the new one has loaded, so a switch never
  // shows the page unstyled in between.
  const link = doc.createElement("link");
  link.rel = "stylesheet";
  link.href = url;
  link.setAttribute(THEME_LINK_ATTRIBUTE, theme.id);
  // Set before the link is inserted: whether a stylesheet holds the first frame is settled when its fetch starts.
  if (options.isRenderBlocking === true) link.setAttribute("blocking", "render");
  return new Promise((resolve) => {
    const settle = (isLoaded: boolean): void => {
      for (const old of existing) old.remove();
      if (!isLoaded) link.remove();
      resolve();
    };
    link.addEventListener("load", () => settle(true), { once: true });
    link.addEventListener("error", () => settle(false), { once: true });
    doc.head.appendChild(link);
  });
}

/** Hear every change of the page's theme, or of whether it is remembered (the shell passes both on to the pages it
 *  frames). */
export function onThemeChanged(listener: ThemeListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Wear the theme a `shell:theme` message names, remembering it unless it is a preview; a malformed message changes
 *  nothing. */
export function wearThemeFromMessage(message: {
  theme?: unknown;
  revision?: unknown;
  isPreview?: unknown;
}): Promise<void> {
  const theme = { id: message.theme, revision: message.revision ?? "" };
  if (!isThemeRef(theme)) return Promise.resolve();
  return wearTheme(theme, document, { isRemembered: message.isPreview !== true });
}

/**
 * Wear whatever theme the parent page wears, for a page framed by a page of its own app on its own origin (the
 * chat root's chat pages), which the shell never messages directly. Answers whether there is such a parent. The
 * parent remembers the theme for the origin both share, and only it knows whether the theme is a preview, so the
 * framed page never remembers it.
 */
export function mirrorParentTheme(): boolean {
  if (window.parent === window) return false;
  let parentRoot: Element;
  try {
    parentRoot = window.parent.document.documentElement;
  } catch {
    return false;
  }
  const follow = (): void => {
    const theme = {
      id: parentRoot.getAttribute(THEME_ATTRIBUTE) ?? STANDARD_THEME_ID,
      revision: parentRoot.getAttribute(THEME_REVISION_ATTRIBUTE) ?? "",
    };
    if (isThemeRef(theme)) void wearTheme(theme, document, { isRemembered: false });
  };
  follow();
  new MutationObserver(follow).observe(parentRoot, {
    attributes: true,
    attributeFilter: [THEME_ATTRIBUTE, THEME_REVISION_ATTRIBUTE],
  });
  return true;
}

/** The theme this origin wore last, as the boot script read it; the standard look when none is remembered. */
export function rememberedTheme(): ThemeRef {
  return readRemembered() ?? STANDARD_THEME;
}

// Test-only: back to a freshly loaded page.
export function resetThemeClientForTests(): void {
  current = STANDARD_THEME;
  isCurrentRemembered = true;
  hasAdoptedBootedTheme = false;
  listeners.clear();
  document.documentElement.removeAttribute(THEME_ATTRIBUTE);
  document.documentElement.removeAttribute(THEME_REVISION_ATTRIBUTE);
  for (const link of themeLinks(document)) link.remove();
  try {
    window.localStorage.removeItem(THEME_STORAGE_KEY);
  } catch {
    // Nothing remembered to forget.
  }
}
