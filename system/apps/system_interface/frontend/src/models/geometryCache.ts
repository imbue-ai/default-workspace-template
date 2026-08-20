/**
 * Persistent store for measured transcript geometry, so a conversation opened
 * again is accurate immediately rather than settling in from an estimate.
 *
 * A completed row's height at a given viewport width is a fact, not a guess: the
 * content is immutable once the turn is done, and the layout is deterministic.
 * Measuring it on every visit is wasted work whose only effect is a visible
 * settle. So it is measured once and remembered.
 *
 * Entries are keyed by agent *and* width bucket, because height is a function of
 * both. Width is bucketed rather than exact so the common layout changes -- a
 * sidebar opening, a panel resizing by a few pixels -- do not throw away a
 * conversation's whole geometry; a genuine change to a different bucket
 * correctly misses and re-measures.
 *
 * IndexedDB rather than localStorage: the tables run to thousands of numbers for
 * a long conversation, and localStorage is synchronous, so reading one would
 * block the main thread during a paint. Every operation here degrades to an
 * in-memory map if IndexedDB is unavailable (private browsing, denied quota) --
 * the transcript must still render, just without the cross-reload benefit.
 */

import type { GeometrySnapshot, RowGeometry } from "./rowGeometry";

const DATABASE_NAME = "si-transcript-geometry";
const DATABASE_VERSION = 1;
const STORE_NAME = "geometry";

/**
 * Width is quantized to this many pixels. Wide enough that a scrollbar
 * appearing or a few pixels of panel resize keeps the cache warm, narrow enough
 * that a real layout change (sidebar open/closed, desktop to mobile) lands in a
 * different bucket and re-measures rather than reusing wrong heights.
 */
export const WIDTH_BUCKET_PX = 64;

/** Entries older than this are discarded on read; a conversation not opened in a
 *  month is not worth the space, and its rendering may well have changed. */
export const ENTRY_TTL_MS = 30 * 24 * 60 * 60 * 1000;

/** Cap on stored conversations, evicted least-recently-used. Bounds the database
 *  for someone who opens a great many chats. */
export const MAX_CACHED_CONVERSATIONS = 50;

export interface CachedGeometry {
  /** `${agentId}:${widthBucket}` */
  key: string;
  rows: RowGeometry[];
  updated_at: number;
}

/** Quantize a viewport width into the bucket its geometry is cached under. */
export function widthBucketFor(width: number): number {
  return Math.max(0, Math.round(width / WIDTH_BUCKET_PX));
}

function cacheKey(agentId: string, widthBucket: number): string {
  return `${agentId}:${widthBucket}`;
}

export interface GeometryCache {
  load(agentId: string, widthBucket: number): Promise<GeometrySnapshot | null>;
  save(agentId: string, widthBucket: number, snapshot: GeometrySnapshot): Promise<void>;
}

/**
 * Open the database, or resolve null when IndexedDB is unusable.
 *
 * Never rejects: a browser that denies storage must degrade to an in-memory
 * cache, not break the transcript.
 */
function openDatabase(): Promise<IDBDatabase | null> {
  return new Promise((resolve) => {
    if (typeof indexedDB === "undefined") {
      resolve(null);
      return;
    }
    let request: IDBOpenDBRequest;
    try {
      request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    } catch {
      resolve(null);
      return;
    }
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(STORE_NAME)) {
        database.createObjectStore(STORE_NAME, { keyPath: "key" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => resolve(null);
    request.onblocked = () => resolve(null);
  });
}

/** Await one IndexedDB request, resolving to null on any failure. */
function awaitRequest<T>(request: IDBRequest<T>): Promise<T | null> {
  return new Promise((resolve) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => resolve(null);
  });
}

/**
 * The object store for one transaction, or null when the database will not open
 * one.
 *
 * A connection that opened successfully can still refuse a transaction later --
 * it throws (rather than erroring a request) once it is closing, or if the store
 * is missing. Caught here so that lands in the in-memory fallback like every
 * other way IndexedDB can be unusable, instead of rejecting out of a cache whose
 * whole contract is that it degrades.
 */
function objectStore(database: IDBDatabase, mode: IDBTransactionMode): IDBObjectStore | null {
  try {
    return database.transaction(STORE_NAME, mode).objectStore(STORE_NAME);
  } catch {
    return null;
  }
}

/**
 * The keys to drop: everything past the TTL, then the least recently updated of
 * what is left once it exceeds the cap.
 *
 * Shared by both backends rather than written twice, because retention is the
 * one respect in which a caller could otherwise tell the database path from the
 * in-memory fallback -- and the fallback is meant to be indistinguishable.
 */
function keysToEvict(entries: CachedGeometry[], now: number): string[] {
  const expired: string[] = [];
  const live: CachedGeometry[] = [];
  for (const entry of entries) {
    if (now - entry.updated_at > ENTRY_TTL_MS) {
      expired.push(entry.key);
    } else {
      live.push(entry);
    }
  }
  if (live.length <= MAX_CACHED_CONVERSATIONS) {
    return expired;
  }
  live.sort((a, b) => a.updated_at - b.updated_at);
  return [...expired, ...live.slice(0, live.length - MAX_CACHED_CONVERSATIONS).map((entry) => entry.key)];
}

/**
 * Drop expired entries and, if still over the cap, the least recently updated
 * ones. Runs after a write, so the bound is maintained without a separate
 * sweep and without blocking the read path.
 */
async function evictIfNeeded(database: IDBDatabase, now: number): Promise<void> {
  const store = objectStore(database, "readwrite");
  if (store === null) {
    return;
  }
  const all = await awaitRequest<CachedGeometry[]>(store.getAll() as IDBRequest<CachedGeometry[]>);
  if (all === null) {
    return;
  }
  for (const key of keysToEvict(all, now)) {
    store.delete(key);
  }
}

/**
 * The cache, backed by IndexedDB where available and by a plain map otherwise.
 *
 * The in-memory fallback is not a degraded special case to reason about at the
 * call site: the interface is identical, and a caller cannot tell which it got.
 */
export function createGeometryCache(now: () => number = () => Date.now()): GeometryCache {
  const memory = new Map<string, CachedGeometry>();
  // Opened once and shared; null once we know IndexedDB is unusable.
  let databasePromise: Promise<IDBDatabase | null> | null = null;

  function database(): Promise<IDBDatabase | null> {
    databasePromise ??= openDatabase();
    return databasePromise;
  }

  // The fallback expires entries on the same terms as the database, so a caller
  // cannot tell the two apart by their behaviour.
  function loadFromMemory(key: string): GeometrySnapshot | null {
    const entry = memory.get(key);
    if (entry === undefined || now() - entry.updated_at > ENTRY_TTL_MS) {
      return null;
    }
    return { rows: entry.rows };
  }

  // Bounded on the same terms as the database, so a session that never gets one
  // does not accumulate a row table per conversation and width.
  function saveToMemory(entry: CachedGeometry): void {
    memory.set(entry.key, entry);
    for (const key of keysToEvict([...memory.values()], now())) {
      memory.delete(key);
    }
  }

  return {
    async load(agentId: string, widthBucket: number): Promise<GeometrySnapshot | null> {
      const key = cacheKey(agentId, widthBucket);
      const db = await database();
      const store = db === null ? null : objectStore(db, "readonly");
      if (store === null) {
        return loadFromMemory(key);
      }
      const entry = await awaitRequest<CachedGeometry>(store.get(key) as IDBRequest<CachedGeometry>);
      if (entry === null || entry === undefined) {
        return null;
      }
      // Expired entries are treated as absent rather than deleted here, so the
      // read path stays a single read-only transaction; the write path's
      // eviction removes them.
      if (now() - entry.updated_at > ENTRY_TTL_MS) {
        return null;
      }
      return { rows: entry.rows };
    },

    async save(agentId: string, widthBucket: number, snapshot: GeometrySnapshot): Promise<void> {
      const entry: CachedGeometry = {
        key: cacheKey(agentId, widthBucket),
        rows: snapshot.rows,
        updated_at: now(),
      };
      const db = await database();
      const store = db === null ? null : objectStore(db, "readwrite");
      if (db === null || store === null) {
        saveToMemory(entry);
        return;
      }
      await awaitRequest(store.put(entry) as IDBRequest<IDBValidKey>);
      await evictIfNeeded(db, now());
    },
  };
}

/**
 * The one cache this page uses, shared by every transcript that reads or writes
 * geometry.
 *
 * There is one database per page and the caps above are caps on *it*: entries
 * are keyed by agent and width, so nothing about a key names the panel that
 * measured it. A cache per panel would open a connection per panel and count
 * its fifty conversations separately, which is not the bound either comment
 * claims. The connection opens lazily on the first read or write and lives as
 * long as the page, so there is nothing to tear down when a panel closes.
 *
 * `createGeometryCache` stays exported for the unit tests, which drive their own
 * instance with an injected clock.
 */
export const sharedGeometryCache: GeometryCache = createGeometryCache();
