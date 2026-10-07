/**
 * Which icon the desktop draws for an app under the theme it wears (docs/system/blueprint/workspace-themes/,
 * section 6.3): the theme's curated icon for the app, else one generated in this workspace (both listed in the
 * catalog's `icons.apps`), else one derived here from the app's standard icon by the theme's `derive`, else the
 * theme's generic program icon. The standard theme draws the app's own icon.
 *
 * A derived icon is rasterized in the page (an SVG drawn onto a canvas at the theme's size, then pixelated,
 * quantized, or turned to two colors) and kept by theme revision, app, and the app's standard icon; until it is
 * ready the fallback shows, and the page redraws once it lands.
 */

import m from "mithril";
import { STANDARD_THEME_ID } from "@imbue/workspace-ui/src/themes/themeClient";
import type { ThemeIcons, ThemeRecord } from "../model/themes";

export type IconSource = "standard" | "theme" | "derived" | "fallback";

export interface ThemedIcon {
  readonly url: string;
  readonly source: Exclude<IconSource, "standard">;
  readonly isPixelated: boolean;
}

let iconTheme: ThemeRecord | null = null;
const derivedByKey = new Map<string, string | null>();
const OPAQUE_ALPHA = 128;
const WHITE: readonly [number, number, number] = [255, 255, 255];
const BLACK: readonly [number, number, number] = [0, 0, 0];

/** Draw every app icon from here on under `theme` (the shell calls this whenever the theme it wears changes). */
export function setIconTheme(theme: ThemeRecord): void {
  iconTheme = theme;
  // Only icons derived under this theme's revision can show again; the rest would pile up with every edit.
  const prefix = derivedKeyPrefix(theme);
  for (const key of derivedByKey.keys()) {
    if (!key.startsWith(prefix)) derivedByKey.delete(key);
  }
}

function derivedKeyPrefix(theme: ThemeRecord): string {
  return `${theme.id}@${theme.revision}/`;
}

/** An app that registers a new standard icon gets a new derived one. */
function derivedKey(theme: ThemeRecord, appName: string, standardMarkup: string): string {
  return `${derivedKeyPrefix(theme)}${appName}/${standardMarkup}`;
}

/** Whether an icon derived under the theme is pixel art: the theme renders pixelated, or its derivation pixelates. */
function isDerivedPixelArt(icons: ThemeIcons): boolean {
  return icons.rendering === "pixelated" || icons.derive === "pixelate";
}

/** The icon the theme draws for an app, or null when the app's standard icon is what shows. `standardMarkupAt`
 *  draws the app's standard icon at a pixel size; a derived icon is drawn from it at the theme's own size, so one
 *  app is derived once whatever size each surface shows it at. */
export function themedIconFor(
  appName: string,
  standardMarkupAt: ((size: number) => string) | null,
): ThemedIcon | null {
  const theme = iconTheme;
  if (theme === null || theme.id === STANDARD_THEME_ID || theme.icons === null) return null;
  const icons = theme.icons;
  const isPixelated = icons.rendering === "pixelated";
  const listed = icons.apps[appName];
  if (listed !== undefined) return { url: listed, source: "theme", isPixelated };
  if (icons.derive !== "none" && standardMarkupAt !== null) {
    const standardMarkup = standardMarkupAt(icons.size);
    const key = derivedKey(theme, appName, standardMarkup);
    const derived = derivedByKey.get(key);
    if (derived !== undefined && derived !== null) {
      return { url: derived, source: "derived", isPixelated: isDerivedPixelArt(icons) };
    }
    if (derived === undefined) startDerivation(key, standardMarkup, icons);
  }
  return icons.fallback_url === null ? null : { url: icons.fallback_url, source: "fallback", isPixelated };
}

function startDerivation(key: string, standardMarkup: string, icons: ThemeIcons): void {
  // Marked as under way, so a redraw while it runs does not start another.
  derivedByKey.set(key, null);
  void deriveIcon(standardMarkup, icons).then((url) => {
    if (url === null) return;
    derivedByKey.set(key, url);
    m.redraw();
  });
}

async function deriveIcon(standardMarkup: string, icons: ThemeIcons): Promise<string | null> {
  const canvas = document.createElement("canvas");
  canvas.width = icons.size;
  canvas.height = icons.size;
  const context = canvas.getContext("2d");
  if (context === null) return null;
  const image = new Image();
  image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(standardMarkup)}`;
  try {
    await image.decode();
  } catch (error) {
    console.warn("[si] could not draw an app's standard icon to derive its themed one", error);
    return null;
  }
  context.imageSmoothingEnabled = !isDerivedPixelArt(icons);
  context.drawImage(image, 0, 0, icons.size, icons.size);
  const pixels = context.getImageData(0, 0, icons.size, icons.size);
  transformPixels(pixels.data, icons);
  context.putImageData(pixels, 0, 0);
  return canvas.toDataURL("image/png");
}

function hexToRgb(hex: string): [number, number, number] {
  return [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)];
}

function nearest(rgb: readonly [number, number, number], palette: readonly (readonly [number, number, number])[]) {
  let best = palette[0];
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const color of palette) {
    const distance = (rgb[0] - color[0]) ** 2 + (rgb[1] - color[1]) ** 2 + (rgb[2] - color[2]) ** 2;
    if (distance < bestDistance) {
      best = color;
      bestDistance = distance;
    }
  }
  return best;
}

/** Levels per channel: the cube root of `maxColors`, so at most that many colors remain, but never fewer than two
 *  (eight colors, even for a smaller count); six when the theme sets no count. */
function levelsFor(maxColors: number | null): number {
  return maxColors === null ? 6 : Math.max(2, Math.floor(Math.cbrt(maxColors)));
}

/**
 * Turn rasterized pixels into the theme's kind of icon, in place: pixel art's alpha becomes all or nothing;
 * `monochrome` makes every opaque pixel the palette's darkest or lightest color by brightness (black and white
 * without a palette); `quantize` maps to the palette, or to at most `max_colors` evenly spaced colors.
 */
export function transformPixels(data: Uint8ClampedArray, icons: ThemeIcons): void {
  const palette = icons.palette.map(hexToRgb);
  const byBrightness = [...palette].sort((a, b) => a[0] + a[1] + a[2] - (b[0] + b[1] + b[2]));
  const dark = byBrightness[0] ?? BLACK;
  const light = byBrightness[byBrightness.length - 1] ?? WHITE;
  const levels = levelsFor(icons.max_colors);
  const step = 255 / (levels - 1);
  const isPixelArt = isDerivedPixelArt(icons);
  for (let index = 0; index < data.length; index += 4) {
    if (isPixelArt) data[index + 3] = data[index + 3] >= OPAQUE_ALPHA ? 255 : 0;
    if (data[index + 3] === 0) continue;
    const rgb: [number, number, number] = [data[index], data[index + 1], data[index + 2]];
    let mapped: readonly [number, number, number] = rgb;
    if (icons.derive === "monochrome") {
      const brightness = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2];
      mapped = brightness < 128 ? dark : light;
    } else if (icons.derive === "quantize") {
      mapped =
        palette.length > 0
          ? nearest(rgb, palette)
          : [Math.round(rgb[0] / step) * step, Math.round(rgb[1] / step) * step, Math.round(rgb[2] / step) * step];
    }
    data[index] = mapped[0];
    data[index + 1] = mapped[1];
    data[index + 2] = mapped[2];
  }
}

// Test-only: forget the theme and every derived icon.
export function resetThemeIconsForTests(): void {
  iconTheme = null;
  derivedByKey.clear();
}
