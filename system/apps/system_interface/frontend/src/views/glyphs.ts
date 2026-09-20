/**
 * The small glyphs the desktop's chrome draws that the shared icon set does not carry: the
 * window controls (minimize, maximize, restore), the kebab, the launcher's plus and app
 * fallback, and the compact launcher's search. Same Feather-style 24x24 frame as ``icons.ts``,
 * produced as SVG strings for ``m.trust``.
 */

import { appIconMarkupForApp } from "./components/appIcon";
import type { AppRecord } from "../model/records";

const XMLNS = "http://www.w3.org/2000/svg";

const GLYPH_PATHS = {
  minimize: '<path d="M5 12h14"/>',
  maximize: '<rect x="4" y="4" width="16" height="16" rx="2"/>',
  restore:
    '<rect x="3" y="8" width="13" height="13" rx="2"/><path d="M8 8V5a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-3"/>',
  kebab:
    '<circle cx="5" cy="12" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="19" cy="12" r="1.5" fill="currentColor" stroke="none"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  app: '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
} as const;

export type GlyphName = keyof typeof GLYPH_PATHS;

export function glyph(name: GlyphName, size: number): string {
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
    `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `${GLYPH_PATHS[name]}</svg>`
  );
}

/** The glyph an app wears everywhere: its own icon, its monogram, or the generic app glyph. */
export function appGlyph(app: Pick<AppRecord, "name" | "icon"> | undefined, size: number): string {
  return appIconMarkupForApp(app, size, glyph("app", size));
}
