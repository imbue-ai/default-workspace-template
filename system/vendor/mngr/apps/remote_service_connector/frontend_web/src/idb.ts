// A tiny promise-flavored IndexedDB wrapper: one database, one object store
// per concern (the DEK, pending creates). Deliberately minimal -- the chrome
// stores single small values, not query workloads.

const DB_NAME = "minds-web";
const DB_VERSION = 1;
const STORE_NAMES = ["dek", "pending-creates"] as const;

export type StoreName = (typeof STORE_NAMES)[number];

let dbPromise: Promise<IDBDatabase> | null = null;

function openDatabase(): Promise<IDBDatabase> {
  if (dbPromise !== null) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      for (const name of STORE_NAMES) {
        if (!request.result.objectStoreNames.contains(name)) {
          request.result.createObjectStore(name);
        }
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  return dbPromise;
}

function awaitRequest<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export interface KeyValueStore {
  get<T>(key: string): Promise<T | undefined>;
  put(key: string, value: unknown): Promise<void>;
  delete(key: string): Promise<void>;
  listKeys(): Promise<string[]>;
}

export async function openStore(name: StoreName): Promise<KeyValueStore> {
  const db = await openDatabase();
  return {
    async get<T>(key: string): Promise<T | undefined> {
      const tx = db.transaction(name, "readonly");
      return awaitRequest(tx.objectStore(name).get(key)) as Promise<
        T | undefined
      >;
    },
    async put(key: string, value: unknown): Promise<void> {
      const tx = db.transaction(name, "readwrite");
      await awaitRequest(tx.objectStore(name).put(value, key));
    },
    async delete(key: string): Promise<void> {
      const tx = db.transaction(name, "readwrite");
      await awaitRequest(tx.objectStore(name).delete(key));
    },
    async listKeys(): Promise<string[]> {
      const tx = db.transaction(name, "readonly");
      const keys = await awaitRequest(tx.objectStore(name).getAllKeys());
      return keys.map((key) => String(key));
    },
  };
}
