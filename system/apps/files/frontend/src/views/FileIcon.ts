/**
 * The icon an entry wears: a folder, or a page with a folded corner and a coloured tag naming its type. An image
 * can stand in a thumbnail of itself instead. Drawn as SVG on a 64-unit grid and scaled to the size asked; below 32px
 * the tag loses its letters, which would not read.
 */

import m from "mithril";

import { urlOf, type Entry } from "../models/server";
import { badgeOf, familyOf, hasImageThumbnail, type FileFamily } from "../models/fileKinds";

// The folder is the Files app icon's own hand-drawn shape (system/apps/files/icon.svg), drawn on that icon's grid
// and fitted to 64 units. Its back shows above a line that slopes down to the right, its front below it with its
// top-left corner rounded, each clipped from the one outline so the edges match exactly and the darker back never
// bleeds through the front's rim.
const FOLDER_OUTLINE =
  "M21.34 15.75C24.52 15.67 26.57 15.75 27.93 15.9C29.52 16.05 30.35 16.89 31.26 18.25C32.17 19.61 32.7 20.29 34.14 20.37C37.16 20.52 41.15 20.22 44.26 20.37C46.83 20.52 48.14 22.49 47.99 24.98C47.76 29.07 47.16 34.22 46.47 38.23C46.02 40.8 44.43 42.09 41.55 42.17C34.89 42.32 26.19 42.32 20.36 42.09C17.56 41.94 16.37 41.03 16.17 38.23C15.67 31.23 16.42 24.53 16.8 20.29C17.03 17.34 18.47 15.83 21.34 15.75Z";

let folderClipCount = 0;

function folderSvg(): string {
  const id = `files-folder-${(folderClipCount += 1)}`;
  return (
    '<g transform="translate(-24 -18.8) scale(1.75)">' +
    `<clipPath id="${id}-back"><path d="M14 10H50V23.6C38 23.05 26 22.45 19.6 22.15C17.3 22.2 16 23.3 15.3 25.8H14Z"/></clipPath>` +
    `<clipPath id="${id}-front"><path d="M15.45 25.6C16.05 23.15 17.35 22 19.6 21.95C26 22.25 38 22.85 50 23.4V44H14V25.6Z"/></clipPath>` +
    `<path d="${FOLDER_OUTLINE}" clip-path="url(#${id}-back)" style="fill:var(--files-folder-back)"/>` +
    `<path d="${FOLDER_OUTLINE}" clip-path="url(#${id}-front)" style="fill:var(--files-folder)"/></g>`
  );
}

// A page in the same hand: no edge quite straight, corners a little uneven, the fold drawn rather than ruled,
// and unshaded.
const PAGE_OUTLINE =
  "M17.2 4.7C24.1 4.25 31.2 4.35 37.5 4.55C39 4.6 40 5.05 41.05 6.1L51.1 16.3C52.05 17.3 52.45 18.35 52.45 19.85C52.65 30.2 51.6 44.1 49.65 55.3C49.4 58.6 47.6 59.9 44.6 60.05C36 60.35 25 59.35 17.6 58.9C14.6 58.65 13 57.15 12.75 54.25C12.25 44.2 12.3 26.1 12.85 9.35C12.95 6.2 14.35 4.85 17.2 4.7Z";
const PAGE_FOLD =
  "M37.75 4.75C38.35 8.9 38.25 12.5 38.5 15.35C38.7 17.55 39.8 18.6 42.05 18.75C45.4 18.95 49.05 18.8 52.3 18.95";

/** The page, white, with its outline and fold. Below 32px the line is drawn heavier and darker, or a page in a list
 *  row would fade into the row. */
function pageSvg(isSmall: boolean): string {
  const color = isSmall ? "var(--c-text-faint)" : "var(--c-border-strong)";
  const width = isSmall ? 4 : 1.5;
  return (
    `<path d="${PAGE_OUTLINE}" style="fill:var(--c-surface);stroke:${color}" stroke-width="${width}" stroke-linejoin="round"/>` +
    `<path d="${PAGE_FOLD}" fill="none" style="stroke:${color}" stroke-width="${width}" stroke-linecap="round" stroke-linejoin="round"/>`
  );
}

const PAGE_LINES_PATH =
  "M20.2 30.3C27 29.9 37 30.1 43.8 29.9M20.1 37.2C27.5 36.9 36.5 37.1 43.9 36.8M20.2 44.1C25.5 43.9 31.5 44.2 37 43.9";

/** The ruled lines on a page with no tag, drawn by hand like the page and leaning 1 degree, as a tag does.
 *  Below 32px they are drawn heavier and darker, or a page in a list row would look blank. */
function pageLinesSvg(isSmall: boolean): string {
  const color = isSmall ? "var(--c-text-faint)" : "var(--c-border-strong)";
  const width = isSmall ? 4.5 : 2.5;
  return (
    `<path transform="rotate(1 32 37)" d="${PAGE_LINES_PATH}" fill="none" style="stroke:${color}" ` +
    `stroke-width="${width}" stroke-linecap="round"/>`
  );
}

/** What a badge in an icon's corner says: a symbolic link, or a share read live (linked) or kept as a copy (synced). */
export type IconBadge = "symlink" | "linked" | "synced" | "syncing";

const SYNC_ARROWS =
  '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>';

const BADGE_GLYPHS: Record<IconBadge, string> = {
  symlink: '<path d="M5 19 19 5"/><path d="M7 5h12v12"/>',
  linked:
    '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
  synced: SYNC_ARROWS,
  syncing: SYNC_ARROWS,
};

/** The disc an icon's badge sits in and the glyph in it, in whole pixels: the glyph centred with an even margin, so
 *  neither lands between pixels and blurs. 12px and 8px at list size, where the link's two loops need the room; the
 *  icon's own proportion from icon size up. */
function badgeMetrics(size: number): { disc: number; glyph: number; right: number; bottom: number; line: number } {
  if (size < 32) return { disc: 12, glyph: 8, right: -4, bottom: -2, line: 1 };
  const disc = Math.max(11, Math.round((size * 19) / 64));
  let glyph = Math.round(disc * 0.6);
  if ((disc - glyph) % 2 !== 0) glyph -= 1;
  return {
    disc,
    glyph,
    right: Math.round((size * 14) / 64 - disc / 2),
    bottom: Math.round((size * 12) / 64 - disc / 2),
    line: (size * 1.25) / 64,
  };
}

/** A badge over an icon's bottom-right corner: a white disc, with a hairline edge from icon size up, holding the
 *  badge's glyph, which turns while a sync is moving. Its own layer rather than part of the icon's drawing, so it sits
 *  on whole pixels at any size. */
export function cornerBadge(badge: IconBadge, size: number): m.Vnode {
  const { disc, glyph, right, bottom, line } = badgeMetrics(size);
  return m(
    "span.files-corner-badge",
    {
      class: `absolute flex items-center justify-center rounded-full bg-surface text-secondary ${size < 32 ? "" : "border border-strong"}`,
      style: `width:${disc}px;height:${disc}px;right:${right}px;bottom:${bottom}px`,
    },
    m.trust(
      `<svg xmlns="http://www.w3.org/2000/svg" width="${glyph}" height="${glyph}" viewBox="0 0 24 24" fill="none" ` +
        `stroke="currentColor" stroke-width="${(line * 24) / glyph}" stroke-linecap="round" stroke-linejoin="round" ` +
        `aria-hidden="true"${badge === "syncing" ? ' class="files-badge-turning motion-safe:animate-[spin_1.6s_linear_infinite]"' : ""}>` +
        `${BADGE_GLYPHS[badge]}</svg>`,
    ),
  );
}

function escapeText(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** How far a type tag leans, clockwise, so it sits on the hand-drawn page as if stuck on by hand. */
const TAG_TILT_DEGREES = 1;

function tagSvg(family: FileFamily, badge: string, withLetters: boolean): string {
  if (!withLetters) {
    // Too small for letters: a broader band of the colour, which still tells one family from another.
    return (
      `<rect x="17" y="33" width="30" height="20" rx="3" transform="rotate(${TAG_TILT_DEGREES} 32 43)" ` +
      `style="fill:var(--files-family-${family})"/>`
    );
  }
  const rect = `<rect x="16" y="38" width="32" height="13" rx="3" style="fill:var(--files-family-${family})"/>`;
  const fontSize = badge.length > 3 ? 7.5 : 8.5;
  const letters =
    badge === ""
      ? ""
      : `<text x="32" y="47.4" text-anchor="middle" font-family="system-ui, sans-serif" font-weight="700" ` +
        `font-size="${fontSize}" letter-spacing="0.3" fill="#fff">${escapeText(badge)}</text>`;
  return `<g transform="rotate(${TAG_TILT_DEGREES} 32 44.5)">${rect}${letters}</g>`;
}

/** The SVG markup of an entry's icon at `size` pixels. */
export function fileIconSvg(name: string, isFolder: boolean, size: number): string {
  let body: string;
  if (isFolder) {
    body = folderSvg();
  } else {
    const family = familyOf(name, false);
    const tagText = badgeOf(name);
    body =
      pageSvg(size < 32) +
      (family === "other" && tagText === "" ? pageLinesSvg(size < 32) : tagSvg(family, tagText, size >= 32));
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 64 64" aria-hidden="true">${body}</svg>`;
}

/** Images whose thumbnail failed to load, so they fall back to the icon instead of a broken image. */
const brokenThumbnails = new Set<string>();

/** Thumbnails are drawn from the image itself; past this size the bytes are not worth it for an icon. */
const MAX_THUMBNAIL_BYTES = 25_000_000;

export interface FileIconAttrs {
  entry: Entry;
  size: number;
  /** Draw an image as a thumbnail of itself. */
  thumbnail?: boolean;
  /** A shared item's own entry: how it reaches the workspace, worn in the corner in place of a symlink's arrow. */
  shareBadge?: IconBadge;
}

export const FileIcon: m.Component<FileIconAttrs> = {
  view({ attrs: { entry, size, thumbnail = false, shareBadge } }) {
    const useThumbnail =
      thumbnail &&
      !entry.isFolder &&
      entry.size <= MAX_THUMBNAIL_BYTES &&
      hasImageThumbnail(entry.name) &&
      !brokenThumbnails.has(entry.path);
    if (useThumbnail) {
      return m(
        "span.file-icon.flex.items-center.justify-center.shrink-0",
        { style: `width:${size}px;height:${size}px` },
        m("img.files-thumbnail", {
          class: "max-h-full max-w-full rounded-[3px] object-contain shadow-raised",
          src: `${urlOf(entry.path)}?t=${entry.mtime}`,
          alt: "",
          loading: "lazy",
          draggable: false,
          onerror: () => brokenThumbnails.add(entry.path),
        }),
      );
    }
    const badge = shareBadge ?? (entry.isSymlink ? "symlink" : null);
    return m(
      "span.file-icon.relative.flex.items-center.justify-center.shrink-0",
      { style: `width:${size}px;height:${size}px`, "data-icon-badge": badge ?? undefined },
      [m.trust(fileIconSvg(entry.name, entry.isFolder, size)), badge === null ? null : cornerBadge(badge, size)],
    );
  },
};
