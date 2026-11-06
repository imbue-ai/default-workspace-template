/** The one thing the viewer keeps across page loads (the TURN spec, section 4.4): that this client's network needed
 *  a relay, so the next connection fetches ``connect-info?relay=1`` first instead of paying the four-second direct
 *  attempt again. */

export const NEEDS_RELAY_KEY = "screen:needs-relay";

/** ``window.localStorage`` when the page may use it; null in a context that forbids it (a sandboxed frame). */
export function pageStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function readNeedsRelay(storage: Storage | null): boolean {
  if (storage === null) return false;
  try {
    return storage.getItem(NEEDS_RELAY_KEY) === "1";
  } catch {
    return false;
  }
}

export function rememberNeedsRelay(storage: Storage | null): void {
  if (storage === null) return;
  try {
    storage.setItem(NEEDS_RELAY_KEY, "1");
  } catch {
    // A full or blocked store costs one slow first connection next time, nothing more.
  }
}
