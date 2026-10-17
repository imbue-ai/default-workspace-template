/**
 * What kind of thing a file is, read off its extension: the family its icon is coloured by, the words the Kind
 * column says, and how a page can show it.
 */

import { extensionOf } from "./paths";

export type FileFamily =
  | "folder"
  | "image"
  | "video"
  | "audio"
  | "pdf"
  | "archive"
  | "code"
  | "data"
  | "text"
  | "document"
  | "spreadsheet"
  | "presentation"
  | "other";

/** How a file page shows a file: in a player or frame of the browser's own, as text, or not at all. */
export type PreviewKind = "image" | "video" | "audio" | "pdf" | "text" | "none";

interface KindRow {
  family: FileFamily;
  label: string;
}

const KINDS: Record<string, KindRow> = {
  png: { family: "image", label: "PNG image" },
  jpg: { family: "image", label: "JPEG image" },
  jpeg: { family: "image", label: "JPEG image" },
  gif: { family: "image", label: "GIF image" },
  webp: { family: "image", label: "WebP image" },
  avif: { family: "image", label: "AVIF image" },
  bmp: { family: "image", label: "Bitmap image" },
  ico: { family: "image", label: "Icon" },
  svg: { family: "image", label: "SVG image" },
  heic: { family: "image", label: "HEIC image" },
  tif: { family: "image", label: "TIFF image" },
  tiff: { family: "image", label: "TIFF image" },
  mp4: { family: "video", label: "MPEG-4 video" },
  m4v: { family: "video", label: "MPEG-4 video" },
  mov: { family: "video", label: "QuickTime movie" },
  webm: { family: "video", label: "WebM video" },
  mkv: { family: "video", label: "Matroska video" },
  avi: { family: "video", label: "AVI video" },
  mp3: { family: "audio", label: "MP3 audio" },
  wav: { family: "audio", label: "WAV audio" },
  ogg: { family: "audio", label: "Ogg audio" },
  m4a: { family: "audio", label: "MPEG-4 audio" },
  flac: { family: "audio", label: "FLAC audio" },
  aac: { family: "audio", label: "AAC audio" },
  pdf: { family: "pdf", label: "PDF document" },
  zip: { family: "archive", label: "ZIP archive" },
  gz: { family: "archive", label: "Gzip archive" },
  tgz: { family: "archive", label: "Gzip archive" },
  tar: { family: "archive", label: "Tar archive" },
  bz2: { family: "archive", label: "Bzip2 archive" },
  xz: { family: "archive", label: "XZ archive" },
  zst: { family: "archive", label: "Zstandard archive" },
  "7z": { family: "archive", label: "7-Zip archive" },
  rar: { family: "archive", label: "RAR archive" },
  py: { family: "code", label: "Python script" },
  ts: { family: "code", label: "TypeScript" },
  tsx: { family: "code", label: "TypeScript" },
  js: { family: "code", label: "JavaScript" },
  jsx: { family: "code", label: "JavaScript" },
  mjs: { family: "code", label: "JavaScript" },
  cjs: { family: "code", label: "JavaScript" },
  html: { family: "code", label: "HTML document" },
  htm: { family: "code", label: "HTML document" },
  css: { family: "code", label: "CSS stylesheet" },
  sh: { family: "code", label: "Shell script" },
  bash: { family: "code", label: "Shell script" },
  zsh: { family: "code", label: "Shell script" },
  rs: { family: "code", label: "Rust source" },
  go: { family: "code", label: "Go source" },
  java: { family: "code", label: "Java source" },
  c: { family: "code", label: "C source" },
  h: { family: "code", label: "C header" },
  cpp: { family: "code", label: "C++ source" },
  rb: { family: "code", label: "Ruby script" },
  sql: { family: "code", label: "SQL" },
  json: { family: "data", label: "JSON" },
  jsonl: { family: "data", label: "JSON Lines" },
  yaml: { family: "data", label: "YAML" },
  yml: { family: "data", label: "YAML" },
  toml: { family: "data", label: "TOML" },
  xml: { family: "data", label: "XML" },
  ini: { family: "data", label: "Settings" },
  conf: { family: "data", label: "Settings" },
  env: { family: "data", label: "Settings" },
  lock: { family: "data", label: "Lockfile" },
  db: { family: "data", label: "Database" },
  sqlite: { family: "data", label: "SQLite database" },
  parquet: { family: "data", label: "Parquet table" },
  md: { family: "text", label: "Markdown" },
  markdown: { family: "text", label: "Markdown" },
  txt: { family: "text", label: "Plain text" },
  log: { family: "text", label: "Log" },
  rst: { family: "text", label: "reStructuredText" },
  csv: { family: "spreadsheet", label: "CSV" },
  tsv: { family: "spreadsheet", label: "TSV" },
  xls: { family: "spreadsheet", label: "Excel spreadsheet" },
  xlsx: { family: "spreadsheet", label: "Excel spreadsheet" },
  numbers: { family: "spreadsheet", label: "Numbers spreadsheet" },
  doc: { family: "document", label: "Word document" },
  docx: { family: "document", label: "Word document" },
  rtf: { family: "document", label: "Rich text" },
  pages: { family: "document", label: "Pages document" },
  odt: { family: "document", label: "OpenDocument text" },
  ppt: { family: "presentation", label: "PowerPoint" },
  pptx: { family: "presentation", label: "PowerPoint" },
  key: { family: "presentation", label: "Keynote" },
};

/** The families whose files the browser itself can draw in an <img>, a player, or a frame. */
const PREVIEW_BY_FAMILY: Partial<Record<FileFamily, PreviewKind>> = {
  image: "image",
  video: "video",
  audio: "audio",
  pdf: "pdf",
};

/** Formats in a family the browser cannot draw: they fall back to text or nothing. */
const UNDRAWABLE = new Set(["heic", "tif", "tiff", "mkv", "avi", "flac"]);

export function familyOf(name: string, isFolder: boolean): FileFamily {
  if (isFolder) return "folder";
  return KINDS[extensionOf(name)]?.family ?? "other";
}

/** The Kind column's words for an entry. */
export function kindLabelOf(name: string, isFolder: boolean): string {
  if (isFolder) return "Folder";
  const extension = extensionOf(name);
  const row = KINDS[extension];
  if (row !== undefined) return row.label;
  return extension === "" ? "Document" : `${extension.toUpperCase()} file`;
}

/** How a file page shows a file; `isText` is dufs's word that the file is small and decodes as text. */
export function previewKindOf(name: string, isText: boolean): PreviewKind {
  const extension = extensionOf(name);
  const family = familyOf(name, false);
  const drawn = UNDRAWABLE.has(extension) ? undefined : PREVIEW_BY_FAMILY[family];
  // An SVG is text too, but it reads best drawn.
  if (drawn !== undefined) return drawn;
  return isText ? "text" : "none";
}

/** Binary formats in otherwise textual families. */
const BINARY_DATA = new Set(["db", "sqlite", "parquet"]);

/** Whether a file is likely text a person would edit: worth offering Edit on before dufs has said so. */
export function isTextLike(name: string): boolean {
  const extension = extensionOf(name);
  if (BINARY_DATA.has(extension)) return false;
  const family = familyOf(name, false);
  return (
    family === "text" ||
    family === "code" ||
    family === "data" ||
    extension === "csv" ||
    extension === "tsv" ||
    extension === "svg" ||
    extension === ""
  );
}

/** Whether an icon may stand in a thumbnail of the image itself. */
export function hasImageThumbnail(name: string): boolean {
  return previewKindOf(name, false) === "image";
}

/** The short tag a document icon wears: the extension, up to four letters. */
export function badgeOf(name: string): string {
  return extensionOf(name).slice(0, 4).toUpperCase();
}
