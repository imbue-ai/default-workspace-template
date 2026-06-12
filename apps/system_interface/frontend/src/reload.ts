/**
 * Reloading the whole system interface into a freshly-built bundle.
 *
 * The backend stamps each served page with a build id (`<meta
 * name="system-interface-build-id">`) and exposes the current build id at
 * `/api/build-id`. After a reveal restarts the service and rebuilds the bundle,
 * the websocket drops and reconnects; on reconnect the frontend compares the id
 * it booted with against the server's current id and, if they differ, reloads
 * the top-level page to pick up the new hashed assets. This is the in-app
 * backstop for the desktop client's health-driven recovery flow, and the only
 * reload path when the UI is viewed in a plain browser.
 */

/** True iff the page should reload: both ids are known and they differ. */
export function shouldReloadForBuild(loadedBuildId: string, currentBuildId: string): boolean {
  if (!loadedBuildId || !currentBuildId) {
    return false;
  }
  return loadedBuildId !== currentBuildId;
}

/** Reload the top-level page that hosts the system interface.
 *
 * In the real deployment the shell IS the top-level page, so `window.top` and
 * `window` are the same frame. We still target `window.top` so the reload
 * reaches the outermost frame if the shell is ever embedded -- but a cross-origin
 * embedding makes `window.top.location` throw a `SecurityError`, so we wrap it
 * and fall back to reloading our own frame. */
export function reloadInterface(): void {
  try {
    const top = window.top;
    if (top !== null) {
      top.location.reload();
      return;
    }
  } catch {
    // Cross-origin top frame: fall through to reloading our own window.
  }
  window.location.reload();
}
