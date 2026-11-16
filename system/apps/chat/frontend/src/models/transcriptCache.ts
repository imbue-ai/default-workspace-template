/**
 * The tail of each chat's transcript as this browser session last held it, so a chat's page paints
 * the conversation the moment it opens instead of waiting on its transcript request. Every chat in
 * the root is its own page, and only a few are kept alive (``root/framePool.ts``), so opening any
 * other chat starts a page with nothing in memory; this is what it shows until the request answers
 * and replaces it.
 *
 * Kept in sessionStorage, which the root's pages share (same origin, same tab), and not
 * localStorage: a transcript can hold anything the user pasted, and it should not outlive the
 * session on disk. Best-effort throughout -- a missing, corrupt or over-quota entry only means the
 * page waits for its request as it would without this.
 */

import type { TranscriptEvent } from "./Response";

const ENTRY_KEY_PREFIX = "transcript-cache:";
const INDEX_KEY = "transcript-cache-index";
const CACHE_VERSION = 1;

// Enough events to fill a tall window; the scroll engine pages in the rest once the real window lands.
export const CACHED_EVENT_LIMIT = 60;
// How many chats keep a cached tail; the one written longest ago goes first.
export const MAX_CACHED_CHATS = 12;
// The most characters one chat's entry may take, so a few chats full of long tool output cannot
// use up the origin's few megabytes. An entry over it keeps fewer events.
export const MAX_ENTRY_CHARS = 400_000;

export interface CachedTranscript {
  readonly events: TranscriptEvent[];
  readonly offset: number;
  readonly total: number;
}

interface StoredEntry {
  version: number;
  events: TranscriptEvent[];
  offset: number;
  total: number;
}

/** The session's storage, or null where there is none (a non-browser environment). */
function sessionStore(): Storage | null {
  return typeof sessionStorage === "undefined" ? null : sessionStorage;
}

function entryKey(chatId: string): string {
  return `${ENTRY_KEY_PREFIX}${chatId}`;
}

function readIndex(storage: Storage): string[] {
  try {
    const parsed: unknown = JSON.parse(storage.getItem(INDEX_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((id): id is string => typeof id === "string") : [];
  } catch {
    return [];
  }
}

function writeIndex(storage: Storage, chatIds: string[]): void {
  storage.setItem(INDEX_KEY, JSON.stringify(chatIds));
}

/** The chat's cached tail, or null when there is none (or it cannot be read). */
export function readCachedTranscript(chatId: string): CachedTranscript | null {
  const storage = sessionStore();
  if (storage === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(storage.getItem(entryKey(chatId)) ?? "null");
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const entry = parsed as Partial<StoredEntry>;
  if (
    entry.version !== CACHE_VERSION ||
    !Array.isArray(entry.events) ||
    entry.events.length === 0 ||
    typeof entry.offset !== "number" ||
    typeof entry.total !== "number"
  ) {
    return null;
  }
  return { events: entry.events, offset: entry.offset, total: entry.total };
}

/** The serialized entry for as many of the last ``events`` as fit, or null when even one event is too large. */
function encodeTail(events: TranscriptEvent[], firstOffset: number, total: number): string | null {
  let count = Math.min(events.length, CACHED_EVENT_LIMIT);
  while (count > 0) {
    const tail = events.slice(events.length - count);
    const entry: StoredEntry = {
      version: CACHE_VERSION,
      events: tail,
      offset: firstOffset + events.length - count,
      total,
    };
    const encoded = JSON.stringify(entry);
    if (encoded.length <= MAX_ENTRY_CHARS) return encoded;
    count = Math.floor(count / 2);
  }
  return null;
}

/** Remember the tail of a window that reaches the transcript's live end. */
export function writeCachedTranscript(
  chatId: string,
  events: TranscriptEvent[],
  firstOffset: number,
  total: number,
): void {
  const storage = sessionStore();
  if (storage === null) return;
  try {
    const encoded = encodeTail(events, firstOffset, total);
    const others = readIndex(storage).filter((id) => id !== chatId);
    if (encoded === null) {
      storage.removeItem(entryKey(chatId));
      writeIndex(storage, others);
      return;
    }
    while (others.length >= MAX_CACHED_CHATS) {
      storage.removeItem(entryKey(others.pop()!));
    }
    for (;;) {
      try {
        storage.setItem(entryKey(chatId), encoded);
        break;
      } catch (error) {
        // Over quota: make room by dropping the oldest other chat, until none is left to drop.
        const oldest = others.pop();
        if (oldest === undefined) throw error;
        storage.removeItem(entryKey(oldest));
      }
    }
    writeIndex(storage, [chatId, ...others]);
  } catch (error) {
    console.warn(`Could not cache the transcript of chat ${chatId}`, error);
  }
}

/** Forget a chat's cached tail. */
export function dropCachedTranscript(chatId: string): void {
  const storage = sessionStore();
  if (storage === null) return;
  try {
    storage.removeItem(entryKey(chatId));
    writeIndex(
      storage,
      readIndex(storage).filter((id) => id !== chatId),
    );
  } catch {
    // Storage refused (privacy mode): nothing was cached either.
  }
}
