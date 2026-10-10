/**
 * The parts of contract 1 (docs/system/blueprint/workspace-themes/, section 4.2): the named elements a theme
 * may style, each marked `data-part="<name>"`. A theme selects nothing else, so a class name stays an app's own
 * business. The validator's copy is `CORE_PARTS` in `system/libs/workspace_themes/src/workspace_themes/contract.py`;
 * a test keeps the two lists equal.
 */

export const PARTS = [
  "desktop",
  "window",
  "window-frame",
  "title-bar",
  "window-icon",
  "window-title",
  "window-control",
  "window-content",
  "taskbar",
  "taskbar-entry",
  "launcher-field",
  "shortcut",
  "shortcut-label",
  "app-icon",
  "button",
  "field",
  "select",
  "menu",
  "menu-item",
  "menu-separator",
  "dialog",
  "dialog-header",
  "dialog-title",
  "dialog-actions",
  "tooltip",
  "badge",
  "tile",
  "list-row",
  "avatar",
] as const;

export type Part = (typeof PARTS)[number];

export const PART_ATTRIBUTE = "data-part";

/** The attribute that marks an element as a contract part, to spread into its attrs. */
export function partAttrs(part: Part): { readonly "data-part": Part } {
  return { "data-part": part };
}

/** The attribute that marks an element as one of an app's declared parts (`<app>.<name>`, section 7). The app
 *  declares the name in its manifest's `[theming]` table. */
export function appPartAttrs(app: string, name: string): { readonly "data-part": string } {
  return { "data-part": `${app}.${name}` };
}

/** Mark an element built outside mithril with a part's attributes (`partAttrs`, `appPartAttrs`, a button's). */
export function applyPartAttrs(element: Element, attrs: Readonly<Record<string, string>>): void {
  for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, value);
}

/** A part's attributes as markup, for an element written as a string (an SVG handed to `m.trust`). */
export function partAttrsMarkup(attrs: Readonly<Record<string, string>>): string {
  return Object.entries(attrs)
    .map(([name, value]) => `${name}="${value.replace(/&/g, "&amp;").replace(/"/g, "&quot;")}"`)
    .join(" ");
}

export const FIELD_PART = partAttrs("field");
export const MENU_PART = partAttrs("menu");
export const MENU_ITEM_PART = partAttrs("menu-item");
export const MENU_SEPARATOR_PART = partAttrs("menu-separator");
export const BADGE_PART = partAttrs("badge");
export const TILE_PART = partAttrs("tile");
export const LIST_ROW_PART = partAttrs("list-row");
