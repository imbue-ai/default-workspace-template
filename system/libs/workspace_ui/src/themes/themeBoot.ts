/**
 * `themeBoot()`: the Vite plugin every app that wears themes adds to its build. It puts a small classic script at
 * the end of the page's `<head>`, after the page's own stylesheet, that wears the theme this origin remembers
 * (themeClient.ts) before the first paint: it marks `<html>` and appends the theme's `<link>` with
 * `blocking="render"`, so the theme is the last stylesheet and the browser holds the first frame until it has
 * loaded rather than flashing the standard look. Before the theme it appends the workspace color's `<link>` the same
 * way, so the page opens in the workspace's own colors.
 */

// This module imports nothing at run time: each app's Vite config loads it in Node, which resolves no extensionless
// relative import, and themeClient.ts takes its constants from here.
import type { Plugin } from "vite";

export const STANDARD_THEME_ID = "standard";
/** The `<html>` attributes that say which theme the page wears and at which revision. */
export const THEME_ATTRIBUTE = "data-ui-theme";
export const THEME_REVISION_ATTRIBUTE = "data-ui-theme-revision";
/** The marker on the theme's `<link>` element. */
export const THEME_LINK_ATTRIBUTE = "data-workspace-theme";
/** The local storage key the boot script and the theme client share. */
export const THEME_STORAGE_KEY = "workspace-theme";
export const THEME_STATIC_PREFIX = "/_static/themes/";
/** The workspace color's stylesheet (docs/system/blueprint/brand-design-system/): the keys the workspace color
 *  stands for, served by every app. It goes before the theme's `<link>`, so a theme always wins over the keys. */
export const WORKSPACE_COLOR_URL = "/_static/workspace_color.css";
/** The marker on the workspace color's `<link>`, the `<html>` attribute naming its revision, and its storage key. */
export const WORKSPACE_COLOR_LINK_ATTRIBUTE = "data-workspace-color";
export const WORKSPACE_COLOR_ATTRIBUTE = "data-workspace-color";
export const WORKSPACE_COLOR_STORAGE_KEY = "workspace-color";
/** A theme id: its folder's name (the workspace-themes plan, section 2). */
export const THEME_ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,47}$/;

/** The boot script's text: self-contained, since it runs before any module. */
export function themeBootScript(): string {
  return `(function () {
  try {
    var color = window.localStorage.getItem(${JSON.stringify(WORKSPACE_COLOR_STORAGE_KEY)}) || "";
    document.documentElement.setAttribute(${JSON.stringify(WORKSPACE_COLOR_ATTRIBUTE)}, color);
    var colorLink = document.createElement("link");
    colorLink.rel = "stylesheet";
    colorLink.setAttribute("blocking", "render");
    colorLink.setAttribute(${JSON.stringify(WORKSPACE_COLOR_LINK_ATTRIBUTE)}, color);
    colorLink.href = ${JSON.stringify(WORKSPACE_COLOR_URL)} + (color ? "?v=" + encodeURIComponent(color) : "");
    document.head.appendChild(colorLink);
  } catch (error) {
    // Storage refused: the page still loads the keys, and the shell names the revision when it greets the page.
  }
  try {
    var stored = JSON.parse(window.localStorage.getItem(${JSON.stringify(THEME_STORAGE_KEY)}) || "null");
    if (!stored || typeof stored.id !== "string" || !${THEME_ID_PATTERN}.test(stored.id)) return;
    if (stored.id === ${JSON.stringify(STANDARD_THEME_ID)}) return;
    var revision = typeof stored.revision === "string" ? stored.revision : "";
    var root = document.documentElement;
    root.setAttribute(${JSON.stringify(THEME_ATTRIBUTE)}, stored.id);
    root.setAttribute(${JSON.stringify(THEME_REVISION_ATTRIBUTE)}, revision);
    var link = document.createElement("link");
    link.rel = "stylesheet";
    link.setAttribute("blocking", "render");
    link.setAttribute(${JSON.stringify(THEME_LINK_ATTRIBUTE)}, stored.id);
    link.href = ${JSON.stringify(THEME_STATIC_PREFIX)} + encodeURIComponent(stored.id) + "/theme.css" +
      (revision ? "?v=" + encodeURIComponent(revision) : "");
    link.onerror = function () { link.remove(); };
    document.head.appendChild(link);
  } catch (error) {
    // A broken memory of a theme leaves the page in the standard look until the shell names one.
  }
})();`;
}

export function themeBoot(): Plugin {
  return {
    name: "workspace-theme-boot",
    transformIndexHtml() {
      return [{ tag: "script", children: themeBootScript(), injectTo: "head" }];
    },
  };
}
