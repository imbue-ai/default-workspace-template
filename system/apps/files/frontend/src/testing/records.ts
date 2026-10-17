/** Record factories for the Files tests. Each takes overrides, so a test spells only what it is about. */

import type { Entry } from "../models/dufs";
import { childPath } from "../models/paths";

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
