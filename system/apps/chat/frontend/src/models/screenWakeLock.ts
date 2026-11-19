/**
 * Keeps the screen awake while the composer dictates (the workspace-voice-input spec, D9). A phone dims and locks
 * after its idle timeout, speaking is not a touch, and a locked screen hides the page, which stops dictation
 * mid-sentence. The Screen Wake Lock API holds the screen on while the lock is wanted and the page is visible.
 *
 * Best effort, with nothing shown to the user: a browser without the API, a frame without the grant, or a refusal
 * (low battery, say) leaves the screen timing out as before. The browser drops the lock itself when the page is
 * hidden, so it is asked for again when the page comes back, if it is still wanted.
 */

/** The part of a ``WakeLockSentinel`` the lock uses. */
export interface WakeLockSentinelLike {
  readonly released: boolean;
  release(): Promise<void>;
}

/** What the lock reads from the page: the API, whether the page is visible, and when that changes. Injected so
 *  a test can hand in its own. */
export interface WakeLockHost {
  /** Ask the browser to keep the screen on; rejects when it will not. */
  request(): Promise<WakeLockSentinelLike>;
  isVisible(): boolean;
  /** Hear every change of the page's visibility; answers the function that stops listening. */
  onVisibilityChange(listener: () => void): () => void;
}

export interface ScreenWakeLock {
  /** Hold the screen on while ``true``; let it go on ``false``. A repeated value changes nothing. */
  setWanted(isWanted: boolean): void;
}

/** The page's own wake-lock host, or null where there is no page (a test without a window) or the browser has no
 *  Screen Wake Lock API. */
export function browserWakeLockHost(): WakeLockHost | null {
  if (typeof navigator === "undefined" || typeof document === "undefined") return null;
  const wakeLock = (navigator as Navigator & { wakeLock?: WakeLock }).wakeLock;
  if (wakeLock === undefined) return null;
  return {
    request: () => wakeLock.request("screen"),
    isVisible: () => document.visibilityState === "visible",
    onVisibilityChange: (listener) => {
      document.addEventListener("visibilitychange", listener);
      return () => document.removeEventListener("visibilitychange", listener);
    },
  };
}

const swallow = (promise: Promise<unknown>): void => {
  promise.catch(() => undefined);
};

/** A lock over ``host``; with no host there is nothing to hold, and every call does nothing. */
export function createScreenWakeLock(host: WakeLockHost | null): ScreenWakeLock {
  if (host === null) return { setWanted: () => undefined };
  let isWanted = false;
  let sentinel: WakeLockSentinelLike | null = null;
  let isRequesting = false;
  let stopListening: (() => void) | null = null;

  const release = (): void => {
    const held = sentinel;
    sentinel = null;
    if (held !== null && !held.released) swallow(held.release());
  };

  const acquire = (): void => {
    if (!isWanted || isRequesting || !host.isVisible()) return;
    if (sentinel !== null && !sentinel.released) return;
    isRequesting = true;
    host.request().then(
      (granted) => {
        isRequesting = false;
        // The lock stopped being wanted while the request was in flight.
        if (!isWanted) {
          swallow(granted.release());
          return;
        }
        sentinel = granted;
      },
      () => {
        isRequesting = false;
      },
    );
  };

  return {
    setWanted(next) {
      if (next === isWanted) return;
      isWanted = next;
      if (next) {
        stopListening ??= host.onVisibilityChange(acquire);
        acquire();
      } else {
        stopListening?.();
        stopListening = null;
        release();
      }
    },
  };
}
