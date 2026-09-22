/**
 * A localStorage for the node test environment, which has none. Modules in every app persist
 * per-chat state through localStorage (the composer's draft, the transcript's scroll position,
 * the fast-mode switch), and their tests need it the same way, so the polyfill lives here beside
 * the other view-test helpers. The store lasts for the test file: a test that writes cleans up
 * after itself, or calls `localStorage.clear()`.
 */

/** Put a Map-backed `Storage` on `globalThis` when the environment has none; a real one (jsdom's)
 *  is left alone. */
export function installLocalStoragePolyfill(): void {
  const store = new Map<string, string>();
  globalThis.localStorage ??= {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
    key: () => null,
    length: 0,
  } as Storage;
}
