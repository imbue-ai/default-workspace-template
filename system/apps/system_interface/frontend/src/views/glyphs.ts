/**
 * The small glyphs the desktop's chrome draws that the shared icon set does not carry: the
 * window controls (minimize, maximize, restore), the kebab, the size menu's zone pictograms,
 * and the launcher's plus and app fallback. Same Feather-style 24x24 frame as ``icons.ts``,
 * produced as SVG strings for ``m.trust``.
 */

import { appIconMarkupForApp } from "./components/appIcon";
import type { AppRecord, Frame } from "../model/records";

const XMLNS = "http://www.w3.org/2000/svg";

const GLYPH_PATHS = {
  minimize: '<path d="M5 12h14"/>',
  maximize: '<rect x="4" y="4" width="16" height="16" rx="2"/>',
  restore:
    '<rect x="3" y="8" width="13" height="13" rx="2"/><path d="M8 8V5a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-3"/>',
  // Dots of r=2 rather than the 1.5 the other glyphs' 2px strokes suggest: a dot reads lighter than
  // a line of the same width, and these are drawn small enough that the thinner one disappears.
  kebab:
    '<circle cx="12" cy="5" r="2" fill="currentColor" stroke="none"/>' +
    '<circle cx="12" cy="12" r="2" fill="currentColor" stroke="none"/>' +
    '<circle cx="12" cy="19" r="2" fill="currentColor" stroke="none"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  app: '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/>',
} as const;

export type GlyphName = keyof typeof GLYPH_PATHS;

export function glyph(name: GlyphName, size: number): string {
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
    `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `${GLYPH_PATHS[name]}</svg>`
  );
}

// The pictogram a size-menu tile draws: the backdrop as the ``maximize`` glyph's rounded square,
// with the part of it the zone fills shaded in. Drawn FROM the zone's own frame, so the picture
// and the placement it performs cannot say different things.
const ZONE_GLYPH_ORIGIN = 4;
const ZONE_GLYPH_EXTENT = 16;

/** The pictogram for a window zone, drawn from the fractional frame it places a window at. */
export function zoneGlyph(frame: Frame, size: number): string {
  const at = (fraction: number): number => ZONE_GLYPH_ORIGIN + ZONE_GLYPH_EXTENT * fraction;
  const span = (fraction: number): number => ZONE_GLYPH_EXTENT * fraction;
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
    `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `<rect x="${ZONE_GLYPH_ORIGIN}" y="${ZONE_GLYPH_ORIGIN}" width="${ZONE_GLYPH_EXTENT}" ` +
    `height="${ZONE_GLYPH_EXTENT}" rx="2"/>` +
    `<rect x="${at(frame.x)}" y="${at(frame.y)}" width="${span(frame.width)}" height="${span(frame.height)}" ` +
    `rx="1" fill="currentColor" stroke="none"/></svg>`
  );
}

/** The glyph an app wears everywhere: its own icon, its monogram, or the generic app glyph. */
export function appGlyph(app: Pick<AppRecord, "name" | "icon"> | undefined, size: number): string {
  return appIconMarkupForApp(app, size, glyph("app", size));
}
