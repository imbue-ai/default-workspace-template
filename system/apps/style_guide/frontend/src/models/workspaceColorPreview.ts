/**
 * The catalog's workspace-color preview: the page wears what any color would make of the standard look, without
 * changing the workspace's own color. The server derives the keys (``/api/workspace-color/preview``, the same
 * derivation the shell uses), and the stylesheet goes in just before the theme's, so a theme still wins over it as
 * it wins over the workspace's own.
 */

import { THEME_LINK_ATTRIBUTE, WORKSPACE_COLOR_LINK_ATTRIBUTE } from "@imbue/workspace-ui/src/themes/themeClient";

/** The colors Imbue Studio offers for a workspace, by the names it gives them. */
export const STUDIO_PALETTE: readonly { readonly name: string; readonly hex: string }[] = [
  { name: "Confusion", hex: "#0b292b" },
  { name: "Courage", hex: "#492222" },
  { name: "Envy", hex: "#3c3d06" },
  { name: "Peace", hex: "#9fbbd3" },
  { name: "Belonging", hex: "#e8a7a8" },
  { name: "Energy", hex: "#cecd0c" },
  { name: "Strength", hex: "#cfc7b3" },
  { name: "Comfort", hex: "#f5d6a0" },
  { name: "Template", hex: "#e9ecd9" },
  { name: "Clarity", hex: "#fcefd4" },
];

export const PREVIEW_STYLE_ATTRIBUTE = "data-workspace-color-preview";
const HEX_PATTERN = /^#[0-9a-f]{6}$/;

let previewed: string | null = null;

/** The color the page is previewing, or null while it wears the workspace's own. */
export function previewedWorkspaceColor(): string | null {
  return previewed;
}

/** ``value`` as a canonical ``#rrggbb``, or null when it is not one. */
export function canonicalHex(value: string): string | null {
  const trimmed = value.trim().toLowerCase();
  const withHash = trimmed.startsWith("#") ? trimmed : `#${trimmed}`;
  return HEX_PATTERN.test(withHash) ? withHash : null;
}

function previewStyle(doc: Document): HTMLStyleElement | null {
  return doc.head.querySelector<HTMLStyleElement>(`style[${PREVIEW_STYLE_ATTRIBUTE}]`);
}

/** Wear the stylesheet ``hex`` would make; a color the server refuses changes nothing. */
export async function previewWorkspaceColor(hex: string, doc: Document = document): Promise<void> {
  const response = await fetch(`/api/workspace-color/preview?hex=${encodeURIComponent(hex)}`);
  if (!response.ok) return;
  const css = await response.text();
  const style = previewStyle(doc) ?? doc.createElement("style");
  style.setAttribute(PREVIEW_STYLE_ATTRIBUTE, hex);
  style.textContent = css;
  const theme = doc.head.querySelector(`link[${THEME_LINK_ATTRIBUTE}]`);
  const ownColor = doc.head.querySelector(`link[${WORKSPACE_COLOR_LINK_ATTRIBUTE}]`);
  if (theme !== null) doc.head.insertBefore(style, theme);
  else if (ownColor !== null) ownColor.after(style);
  else doc.head.appendChild(style);
  previewed = hex;
}

/** Back to the workspace's own color. */
export function stopPreviewingWorkspaceColor(doc: Document = document): void {
  previewStyle(doc)?.remove();
  previewed = null;
}
