/**
 * The Files server (system/apps/files/src/files_app/server.py). It serves the filesystem root, and a computer's shared
 * files under `/__remote/<device>/`, and speaks plain HTTP and WebDAV verbs on a path's own URL: `?json` describes a
 * folder or file, `?zip` archives a folder, PUT writes a file, MKCOL makes a folder, MOVE renames, DELETE removes.
 * Every folder and file page it serves is this app's index.html with the path's description, base64 JSON, filled
 * into `#index-data`.
 */

import { childPath, encodePath } from "./paths";

/** One entry of a folder as the server lists it. A folder's `size` is how many entries it holds; `mtime` is in
 *  milliseconds. */
export interface EntryRecord {
  name: string;
  is_folder: boolean;
  is_symlink: boolean;
  size: number;
  mtime: number;
}

/** What the server says of a path: a folder and its entries, or a file and whether it is small and reads as text. */
export type PageData = { kind: "folder"; entries: EntryRecord[] } | { kind: "file"; is_text: boolean };

/** An entry of a folder as the app holds it. */
export interface Entry {
  /** The name in its folder. */
  name: string;
  path: string;
  isFolder: boolean;
  isSymlink: boolean;
  /** Bytes for a file, entries for a folder. */
  size: number;
  mtime: number;
}

export class ServerError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export function entryOf(folder: string, record: EntryRecord): Entry {
  return {
    name: record.name,
    path: childPath(folder, record.name, record.is_folder),
    isFolder: record.is_folder,
    isSymlink: record.is_symlink,
    size: record.size,
    mtime: record.mtime,
  };
}

/** The page data the server filled into this document, or null outside the server (a test, the dev server). */
export function readPageData(document: Document): PageData | null {
  const template = document.getElementById("index-data");
  const encoded = template?.innerHTML.trim() ?? "";
  if (encoded === "" || encoded.startsWith("__")) return null;
  const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes)) as PageData;
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

async function failureOf(response: Response, what: string): Promise<ServerError> {
  let detail: string;
  try {
    detail = (await response.text()).trim();
  } catch {
    detail = "";
  }
  const reason = detail === "" || detail.startsWith("<") ? response.statusText || `error ${response.status}` : detail;
  return new ServerError(`${what}: ${reason}`, response.status);
}

async function send(url: string, init: RequestInit, what: string): Promise<Response> {
  const response = await fetch(url, init);
  if (!response.ok) throw await failureOf(response, what);
  return response;
}

export interface Listing {
  /** False for a folder that is not there. */
  exists: boolean;
  entries: Entry[];
}

export async function listFolder(folder: string, signal?: AbortSignal): Promise<Listing> {
  const response = await fetch(`${urlOf(folder)}?json`, { signal });
  if (response.status === 404) return { exists: false, entries: [] };
  if (!response.ok) throw await failureOf(response, `Couldn't open ${folder}`);
  const data = (await response.json()) as PageData;
  if (data.kind !== "folder") throw new ServerError(`Couldn't open ${folder}: it is a file`, 400);
  return { exists: true, entries: data.entries.map((record) => entryOf(folder, record)) };
}

/** Whether anything, file or folder, is at `path`. Asked without the trailing slash, so it is the same question for
 *  a file and a folder, in the workspace or on a computer. */
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

/** PUT a file with progress, which fetch cannot report. The server creates the folders above it as needed. */
export function uploadFile(path: string, file: Blob, onProgress: (loaded: number) => void): Upload {
  const request = new XMLHttpRequest();
  const done = new Promise<void>((resolve, reject) => {
    request.upload.addEventListener("progress", (event) => onProgress(event.loaded));
    request.addEventListener("load", () => {
      if (request.status >= 200 && request.status < 300) resolve();
      else reject(new ServerError(request.responseText.trim() || request.statusText, request.status));
    });
    request.addEventListener("error", () => reject(new ServerError("The connection was lost", 0)));
    request.addEventListener("abort", () => reject(new ServerError("Canceled", 0)));
  });
  request.open("PUT", urlOf(path));
  request.send(file);
  return { done, abort: () => request.abort() };
}
