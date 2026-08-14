// Deep-link support for the shell: ``?open_app=<name>`` on the shell origin
// asks this client to open (or focus) that app's tab after the initial layout
// mounts. The desktop client's cross-workspace app selector builds such URLs
// (riding its ``/goto/<host-id>/`` cookie bridge). The parameter is left in the
// URL once read -- re-reading it only re-focuses the tab, and keeping it is
// what lets a reload retry a link that arrived before the app registered.
// Pure helpers only -- the dockview side lives in ``DockviewWorkspace.ts``
// (``consumeOpenAppDeepLink``).

/** The requested app name in ``query`` (a URL query string, with or without its
 *  leading ``?``), or null. Named for the URL component, not for searching --
 *  this reads a deep link's parameter and has nothing to do with finding
 *  anything. */
export function openAppNameFromQuery(query: string): string | null {
  const params = new URLSearchParams(query.startsWith("?") ? query.slice(1) : query);
  const name = params.get("open_app");
  return name !== null && name !== "" ? name : null;
}
