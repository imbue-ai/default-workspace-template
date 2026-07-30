/**
 * Reloading the whole system interface into the code currently on disk.
 *
 * Every reveal of a changed interface ends by broadcasting a
 * `reload_system_interface` layout op, through
 * `system/scripts/refresh_workspace_view.py` -- for a backend-only change too,
 * not just a rebuilt bundle. The dockview shell handles that op by calling
 * `reloadInterface()`, which reloads the top-level page so the browser picks up
 * the new hashed assets (and any change to the shell chrome itself),
 * transitively reloading every child chat iframe.
 */

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
