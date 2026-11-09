/**
 * Where an engine's checked files are kept between loads (the workspace-voice-input spec, D6): the browser's own
 * Cache Storage, one entry per pinned URL on the workspace's origin, which the browser evicts under storage
 * pressure as it does any site's. Nothing read back is trusted: the download checks every file against its
 * pinned size and hash again, so an entry that was evicted, cut short, or tampered with is downloaded afresh.
 */

import { describeError } from "./download";

export interface PinnedFileStore {
  /** The bytes kept under ``url``, or null for none. */
  read(url: string): Promise<ArrayBuffer | null>;
  /** Keep ``bytes`` under ``url`` for next time; a store that cannot keeps nothing and says so. */
  write(url: string, bytes: ArrayBuffer): Promise<void>;
  /** Drop every entry whose URL starts with ``prefix`` and is not in ``keep``: the engine's other variants, and
   *  its files at an earlier commit. */
  prune(prefix: string, keep: ReadonlySet<string>): Promise<void>;
}

export const PINNED_FILE_CACHE_NAME = "workspace-voice";

/** The part of the Cache API the store uses, so a test can hand in its own. */
export interface CacheLike {
  match(url: string): Promise<Response | undefined>;
  put(url: string, response: Response): Promise<void>;
  delete(url: string): Promise<boolean>;
  keys(): Promise<readonly { url: string }[]>;
}

export interface CacheStorageLike {
  open(name: string): Promise<CacheLike>;
}

/** A store that keeps nothing: every load downloads. */
export const NO_FILE_STORE: PinnedFileStore = {
  read: async () => null,
  write: async () => undefined,
  prune: async () => undefined,
};

export function cacheStorageFileStore(storage: CacheStorageLike): PinnedFileStore {
  const cache = (): Promise<CacheLike> => storage.open(PINNED_FILE_CACHE_NAME);
  return {
    async read(url) {
      try {
        const hit = await (await cache()).match(url);
        return hit === undefined ? null : await hit.arrayBuffer();
      } catch (error) {
        console.warn(`The kept speech model files could not be read: ${describeError(error)}`);
        return null;
      }
    },
    async write(url, bytes) {
      try {
        await (await cache()).put(url, new Response(bytes));
      } catch (error) {
        // Out of quota, most likely: this load has the bytes, and the next one downloads them again.
        console.warn(`A speech model file could not be kept for next time: ${describeError(error)}`);
      }
    },
    async prune(prefix, keep) {
      try {
        const opened = await cache();
        for (const request of await opened.keys()) {
          if (request.url.startsWith(prefix) && !keep.has(request.url)) await opened.delete(request.url);
        }
      } catch (error) {
        console.warn(`The kept speech model files could not be pruned: ${describeError(error)}`);
      }
    },
  };
}

/** This realm's store: its Cache Storage, or nothing where the realm has none. */
export function defaultPinnedFileStore(): PinnedFileStore {
  const storage = (globalThis as unknown as { caches?: CacheStorageLike }).caches;
  return storage === undefined ? NO_FILE_STORE : cacheStorageFileStore(storage);
}
