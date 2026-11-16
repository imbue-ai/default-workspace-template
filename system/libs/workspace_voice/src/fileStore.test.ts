import { afterEach, describe, expect, it, vi } from "vitest";
import { NO_FILE_STORE, PINNED_FILE_CACHE_NAME, cacheStorageFileStore, defaultPinnedFileStore } from "./fileStore";
import type { CacheLike, CacheStorageLike } from "./fileStore";

/** A Cache Storage of one cache, kept in a map; ``failPut`` makes every put fail as a full disk would. */
function fakeStorage(options: { failPut?: boolean } = {}) {
  const entries = new Map<string, Uint8Array>();
  const opened: string[] = [];
  const cache: CacheLike = {
    async match(url) {
      const bytes = entries.get(url);
      return bytes === undefined ? undefined : new Response(bytes.slice());
    },
    async put(url, response) {
      if (options.failPut) throw Object.assign(new Error("quota"), { name: "QuotaExceededError" });
      entries.set(url, new Uint8Array(await response.arrayBuffer()));
    },
    async delete(url) {
      return entries.delete(url);
    },
    async keys() {
      return [...entries.keys()].map((url) => ({ url }));
    },
  };
  const storage: CacheStorageLike = {
    async open(name) {
      opened.push(name);
      return cache;
    },
  };
  return { storage, entries, opened };
}

const REPO = "https://huggingface.co/onnx-community/moonshine-base-ONNX/";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the Cache Storage file store", () => {
  it("keeps bytes under their URL in the library's own cache and reads them back", async () => {
    const { storage, opened } = fakeStorage();
    const store = cacheStorageFileStore(storage);
    expect(await store.read(`${REPO}resolve/abc/config.json`)).toBeNull();
    await store.write(`${REPO}resolve/abc/config.json`, new TextEncoder().encode("{}").buffer as ArrayBuffer);
    expect(new TextDecoder().decode((await store.read(`${REPO}resolve/abc/config.json`)) ?? undefined)).toBe("{}");
    expect(new Set(opened)).toEqual(new Set([PINNED_FILE_CACHE_NAME]));
  });

  it("prunes the engine's other entries and leaves other engines' alone", async () => {
    const { storage, entries } = fakeStorage();
    const store = cacheStorageFileStore(storage);
    const keep = `${REPO}resolve/abc/onnx/encoder_model_quantized.onnx`;
    for (const url of [keep, `${REPO}resolve/abc/onnx/encoder_model.onnx`, `${REPO}resolve/old/config.json`]) {
      await store.write(url, new ArrayBuffer(1));
    }
    await store.write("https://huggingface.co/other/parakeet/resolve/x/config.json", new ArrayBuffer(1));
    await store.prune(REPO, new Set([keep]));
    expect([...entries.keys()].sort()).toEqual([keep, "https://huggingface.co/other/parakeet/resolve/x/config.json"]);
  });

  it("goes on when the browser refuses to keep a file, and reads nothing it cannot read", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { storage } = fakeStorage({ failPut: true });
    const store = cacheStorageFileStore(storage);
    await expect(store.write(`${REPO}resolve/abc/config.json`, new ArrayBuffer(1))).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalledTimes(1);
    const broken = cacheStorageFileStore({
      open: async () => {
        throw new Error("no storage in this context");
      },
    });
    expect(await broken.read(`${REPO}resolve/abc/config.json`)).toBeNull();
  });

  it("keeps nothing where the realm has no Cache Storage", async () => {
    vi.stubGlobal("caches", undefined);
    const store = defaultPinnedFileStore();
    expect(store).toBe(NO_FILE_STORE);
    await store.write("x", new ArrayBuffer(1));
    expect(await store.read("x")).toBeNull();
    vi.unstubAllGlobals();
  });
});
