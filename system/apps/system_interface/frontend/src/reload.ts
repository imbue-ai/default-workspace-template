/**
 * Reloading the whole system interface into the code currently on disk.
 *
 * Every reveal of a changed interface bumps an *epoch*
 * (`system/scripts/refresh_workspace_view.py` writes `data/.state/view_epoch`)
 * and then broadcasts a `reload_system_interface` layout op. Two ways in, for
 * one reason: the broadcast is a live fan-out with no replay, so it only
 * reaches browsers connected at that instant, and a services restart leaves
 * every browser on reconnect backoff for a while after the server is back.
 *
 * - Connected now: the dockview shell handles the op by calling
 *   `reloadInterface()`.
 * - Reconnecting later: the server sends its epoch on WebSocket connect, and a
 *   page whose loaded epoch is older reloads itself (see `shouldReloadForEpoch`).
 *
 * Either way the reload is of the top-level page, so the browser picks up the
 * new hashed assets (and any change to the shell chrome itself), transitively
 * reloading every child chat iframe.
 */

/** Whether a page loaded at `loadedEpoch` should reload now that the server
 * reports `serverEpoch`.
 *
 * An empty `serverEpoch` means nothing has ever been revealed in this
 * workspace, which is not a reason to reload. An empty `loadedEpoch` means this
 * page predates the epoch being served at all -- reloading is precisely the fix,
 * and once reloaded the document carries the current epoch, so this cannot
 * loop. */
export function shouldReloadForEpoch(serverEpoch: string, loadedEpoch: string): boolean {
  if (serverEpoch === "") {
    return false;
  }
  return serverEpoch !== loadedEpoch;
}

/** Reload the top-level page that hosts the system interface.
 *
 * In the real deployment the shell IS the top-level page, so `window.top` and
 * `window` are the same frame. We still target `window.top` so the reload
 * reaches the outermost frame if the shell is ever embedded -- but a cross-origin
 * embedding makes `window.top.location` throw a `SecurityError`, so we wrap it
 * and fall back to reloading our own frame.
 *
 * This cannot drop the browser's HTTP cache: `location.reload(true)` is a
 * Firefox-only extension, and there is no portable equivalent. Freshness is
 * instead guaranteed from the server side -- the shell document is served
 * `Cache-Control: no-store` (see `_html_response` in `server.py`) and the
 * assets it links are content-hashed, so a plain reload always lands on the
 * current bundle. Callers that need the cache itself dropped (the Minds app,
 * which owns the browser session) do that around this reload rather than in
 * it. */
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
