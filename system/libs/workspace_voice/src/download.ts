/**
 * Downloading an engine's pinned files (the workspace-voice-input spec, D6): straight from Hugging Face at the
 * engine's commit, each file checked against its pinned size and SHA-256 before anything is handed to the engine,
 * and kept only in memory for the engine to load. Runs in the engine worker, where ``fetch`` and ``crypto.subtle``
 * are the worker's own.
 */

import type { EngineSpec, PinnedFile } from "./engines";
import { pinnedFileUrl } from "./engines";

export type FetchLike = (url: string) => Promise<Response>;
export type DigestLike = (data: ArrayBuffer) => Promise<ArrayBuffer>;

export interface DownloadOptions {
  fetch?: FetchLike;
  digest?: DigestLike;
  /** Called as bytes arrive, with the bytes of every file so far against the bytes of every file. */
  onProgress?: (loadedBytes: number, totalBytes: number) => void;
}

/** A download that could not be trusted: names the file, so the notice can. Nothing of it is kept. */
export class ModelDownloadError extends Error {
  readonly file: PinnedFile;

  constructor(file: PinnedFile, message: string) {
    super(message);
    this.name = "ModelDownloadError";
    this.file = file;
  }
}

const digestWithSubtle: DigestLike = (data) => crypto.subtle.digest("SHA-256", data);

function hexOf(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function readExactly(
  file: PinnedFile,
  response: Response,
  onChunk: (bytes: number) => void,
): Promise<ArrayBuffer> {
  const bytes = new Uint8Array(file.bytes);
  let received = 0;
  const take = (chunk: Uint8Array): void => {
    if (received + chunk.length > file.bytes) {
      throw new ModelDownloadError(file, `${file.path} is longer than its pinned ${file.bytes} bytes`);
    }
    bytes.set(chunk, received);
    received += chunk.length;
    onChunk(chunk.length);
  };
  if (response.body === null) {
    take(new Uint8Array(await response.arrayBuffer()));
  } else {
    const reader = response.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      take(value);
    }
  }
  if (received !== file.bytes) {
    throw new ModelDownloadError(file, `${file.path} is ${received} bytes, not its pinned ${file.bytes}`);
  }
  return bytes.buffer;
}

/**
 * Download ``files`` of ``spec`` one after another; answers each file's bytes by its path. A file that cannot be
 * fetched, is not its pinned size, or does not hash to its pinned SHA-256 raises ``ModelDownloadError`` naming it,
 * and nothing downloaded so far is kept.
 */
export async function downloadPinnedFiles(
  spec: EngineSpec,
  files: readonly PinnedFile[],
  options: DownloadOptions = {},
): Promise<Map<string, ArrayBuffer>> {
  const fetchFile = options.fetch ?? ((url: string) => fetch(url));
  const digest = options.digest ?? digestWithSubtle;
  const totalBytes = files.reduce((total, file) => total + file.bytes, 0);
  let loadedBytes = 0;
  const downloaded = new Map<string, ArrayBuffer>();
  for (const file of files) {
    const url = pinnedFileUrl(spec, file);
    let response: Response;
    try {
      response = await fetchFile(url);
    } catch (error) {
      throw new ModelDownloadError(file, `${file.path} could not be downloaded: ${describeError(error)}`);
    }
    if (!response.ok) {
      throw new ModelDownloadError(file, `${file.path} could not be downloaded (HTTP ${response.status})`);
    }
    const buffer = await readExactly(file, response, (chunkBytes) => {
      loadedBytes += chunkBytes;
      options.onProgress?.(loadedBytes, totalBytes);
    });
    const actual = hexOf(await digest(buffer));
    if (actual !== file.sha256) {
      throw new ModelDownloadError(file, `${file.path} does not match its pinned SHA-256`);
    }
    downloaded.set(file.path, buffer);
  }
  return downloaded;
}

export function describeError(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  return "unknown error";
}
