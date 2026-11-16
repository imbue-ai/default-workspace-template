// The app's server: read and write one file, list markdown files.

export interface FileRecord {
  path: string;
  name: string;
  text: string;
  sha256: string;
  size: number;
  mtime: number;
  is_writable: boolean;
}

export interface FileVersion {
  sha256: string;
  mtime: number;
}

export interface FoundFile {
  path: string;
  name: string;
  size: number;
  mtime: number;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: Record<string, unknown>,
  ) {
    super(message);
  }
}

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { cache: "no-store", ...init });
  let body: Record<string, unknown>;
  try {
    body = await response.json();
  } catch {
    body = {};
  }
  if (!response.ok) {
    throw new ApiError(String(body["detail"] ?? `The server answered ${response.status}`), response.status, body);
  }
  return body as T;
}

export function readFile(path: string): Promise<FileRecord> {
  return call(`/api/file?path=${encodeURIComponent(path)}`);
}

export function fileVersion(path: string): Promise<FileVersion> {
  return call(`/api/file/version?path=${encodeURIComponent(path)}`);
}

export function writeFile(path: string, text: string, baseSha256: string | null): Promise<FileVersion> {
  return call("/api/file", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, text, base_sha256: baseSha256 }),
  });
}

export function listFiles(): Promise<{ files: FoundFile[]; root: string }> {
  return call("/api/files");
}

export function downloadUrl(path: string): string {
  return `/api/file/raw?path=${encodeURIComponent(path)}&download=1`;
}
