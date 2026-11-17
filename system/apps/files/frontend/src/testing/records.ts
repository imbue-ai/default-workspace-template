/** Record factories for the Files tests. Each takes overrides, so a test spells only what it is about. */

import type { Entry } from "../models/server";
import { childPath } from "../models/paths";
import { readFailure, type RequestKind } from "@imbue/workspace-ui/src/models/failure";
import { HttpError } from "@imbue/workspace-ui/src/models/http";

export function fileEntry(folder: string, name: string, overrides: Partial<Entry> = {}): Entry {
  return {
    name,
    path: childPath(folder, name, false),
    isFolder: false,
    isSymlink: false,
    size: 10,
    mtime: 0,
    ...overrides,
  };
}

export function folderEntry(folder: string, name: string, overrides: Partial<Entry> = {}): Entry {
  return {
    name,
    path: childPath(folder, name, true),
    isFolder: true,
    isSymlink: false,
    size: 0,
    mtime: 0,
    ...overrides,
  };
}

/** A Storage held in memory. */
export function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => void values.delete(key),
    setItem: (key, value) => void values.set(key, value),
  };
}

/** The ``HttpError`` the request layer throws for an answer of `status` carrying `body` (null for a body that was not
 *  JSON, or a status 0 for a request nothing answered). */
export function httpFailure(
  status: number,
  body: Record<string, unknown> | null,
  requestKind: RequestKind = "change",
): HttpError {
  return new HttpError(readFailure({ status, body, rawText: body === null ? "" : JSON.stringify(body), requestKind }));
}
