/**
 * The file server's side of the app. dufs (system/scripts/install_dufs.sh) serves the filesystem root with every
 * operation allowed, and speaks plain HTTP and WebDAV verbs on a path's own URL: `?json` lists a folder, `?q=` searches
 * under it, `?zip` archives it, PUT writes a file, MKCOL makes a folder, MOVE renames, DELETE removes. Every page it
 * serves is this app's index.html with the page's data, base64 JSON, filled into `#index-data`.
 */

import { childPath, encodePath } from "./paths";

export type DufsPathType = "Dir" | "SymlinkDir" | "File" | "SymlinkFile";

/** One entry of a dufs listing. A folder's `size` is how many entries it holds; `mtime` is in milliseconds. */
export interface DufsPathItem {
  path_type: DufsPathType;
  name: string;
  mtime: number;
  size: number;
}

/** What dufs fills into a page: a folder's listing (`Index`), or the file a `?view` / `?edit` page is for. */
export interface DufsPageData {
  href: string;
  kind: "Index" | "View" | "Edit";
  uri_prefix: string;
  allow_upload: boolean;
  allow_delete: boolean;
  allow_search?: boolean;
  allow_archive?: boolean;
  dir_exists?: boolean;
  paths?: DufsPathItem[];
  /** On a file page: whether the file is small and decodes as text. */
  editable?: boolean;
}

/** An entry of a folder as the app holds it. */
export interface Entry {
  /** The name in its folder; a search result's is its path below the folder searched. */
  name: string;
  path: string;
  isFolder: boolean;
  isSymlink: boolean;
  /** Bytes for a file, entries for a folder. */
  size: number;
  mtime: number;
}

export class DufsError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export function entryOf(folder: string, item: DufsPathItem): Entry {
  const isFolder = item.path_type === "Dir" || item.path_type === "SymlinkDir";
  return {
    name: item.name,
    path: childPath(folder, item.name, isFolder),
    isFolder,
    isSymlink: item.path_type.startsWith("Symlink"),
    size: item.size,
    mtime: item.mtime,
  };
}

/** The page data dufs filled into this document, or null outside dufs (a test, the dev server). */
export function readPageData(document: Document): DufsPageData | null {
  const template = document.getElementById("index-data");
  const encoded = template?.innerHTML.trim() ?? "";
  if (encoded === "" || encoded.startsWith("__")) return null;
  const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes)) as DufsPageData;
}

/** The URL of a path's own content: a file's bytes, a folder's page. */
export function urlOf(path: string): string {
  return encodePath(path);
}

/** A file's page in the app: `view` shows it, `edit` opens it in the editor. */
export function pageUrlOf(path: string, page: "view" | "edit"): string {
  return `${urlOf(path)}?${page}`;
}

/** Where a download of a path comes from: the file itself, or a folder as a zip. */
export function downloadUrlOf(path: string, isFolder: boolean): string {
  return isFolder ? `${urlOf(path)}?zip` : urlOf(path);
}

async function failureOf(response: Response, what: string): Promise<DufsError> {
  let detail: string;
  try {
    detail = (await response.text()).trim();
  } catch {
    detail = "";
  }
  const reason = detail === "" || detail.startsWith("<") ? response.statusText || `error ${response.status}` : detail;
  return new DufsError(`${what}: ${reason}`, response.status);
}

async function send(url: string, init: RequestInit, what: string): Promise<Response> {
  const response = await fetch(url, init);
  if (!response.ok) throw await failureOf(response, what);
  return response;
}

export interface Listing {
  /** False for a folder that is not there: dufs lists it as empty rather than failing. */
  exists: boolean;
  entries: Entry[];
}

export async function listFolder(folder: string, signal?: AbortSignal): Promise<Listing> {
  const response = await send(`${urlOf(folder)}?json`, { signal }, `Couldn't open ${folder}`);
  const data = (await response.json()) as DufsPageData;
  return { exists: data.dir_exists !== false, entries: (data.paths ?? []).map((item) => entryOf(folder, item)) };
}

/** Everything under `folder` whose name contains `query`, its name the path below `folder`. */
export async function searchFolder(folder: string, query: string, signal?: AbortSignal): Promise<Entry[]> {
  const params = new URLSearchParams({ q: query });
  const response = await send(`${urlOf(folder)}?${params.toString()}&json`, { signal }, "Search failed");
  const data = (await response.json()) as DufsPageData;
  return (data.paths ?? []).map((item) => entryOf(folder, item));
}

/** Whether anything, file or folder, is at `path`. Asked without the trailing slash: dufs answers any path that ends
 *  in one as a folder, there or not. */
export async function exists(path: string): Promise<boolean> {
  const response = await fetch(urlOf(path.replace(/\/+$/, "")), { method: "HEAD" });
  return response.ok;
}

export async function deletePath(path: string): Promise<void> {
  await send(urlOf(path), { method: "DELETE" }, `Couldn't delete ${path}`);
}

export async function movePath(from: string, to: string): Promise<void> {
  const destination = new URL(urlOf(to), window.location.href).href;
  await send(urlOf(from), { method: "MOVE", headers: { Destination: destination } }, `Couldn't move ${from}`);
}

export async function createFolder(path: string): Promise<void> {
  await send(urlOf(path), { method: "MKCOL" }, `Couldn't create ${path}`);
}

export async function writeFile(path: string, body: BodyInit): Promise<void> {
  await send(urlOf(path), { method: "PUT", body }, `Couldn't save ${path}`);
}

export async function readText(path: string, signal?: AbortSignal, maxBytes?: number): Promise<string> {
  const headers: Record<string, string> = maxBytes === undefined ? {} : { Range: `bytes=0-${maxBytes - 1}` };
  const response = await send(urlOf(path), { signal, headers }, `Couldn't read ${path}`);
  return response.text();
}

export interface Upload {
  done: Promise<void>;
  abort(): void;
}

/** PUT a file with progress, which fetch cannot report. dufs creates the folders above it as needed. */
export function uploadFile(path: string, file: Blob, onProgress: (loaded: number) => void): Upload {
  const request = new XMLHttpRequest();
  const done = new Promise<void>((resolve, reject) => {
    request.upload.addEventListener("progress", (event) => onProgress(event.loaded));
    request.addEventListener("load", () => {
      if (request.status >= 200 && request.status < 300) resolve();
      else reject(new DufsError(request.responseText.trim() || request.statusText, request.status));
    });
    request.addEventListener("error", () => reject(new DufsError("The connection was lost", 0)));
    request.addEventListener("abort", () => reject(new DufsError("Canceled", 0)));
  });
  request.open("PUT", urlOf(path));
  request.send(file);
  return { done, abort: () => request.abort() };
}
