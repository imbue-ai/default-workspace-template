// Deep-link support for the shell: ``?open_app=<name>`` on the shell origin
// asks this client to open (or focus) that app's tab after the initial layout
// mounts. The desktop client's cross-workspace app selector builds such URLs
// (riding its ``/goto/<host-id>/`` cookie bridge). The parameter is left in the
// URL once read -- re-reading it only re-focuses the tab, and keeping it is
// what lets a reload retry a link that arrived before the app registered.
// Pure helpers only -- the dockview side lives in ``DockviewWorkspace.ts``
// (``consumeOpenAppDeepLink``).

/** The requested app name in ``search`` (a ``location.search`` string), or null. */
export function openAppNameFromSearch(search: string): string | null {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const name = params.get("open_app");
  return name !== null && name !== "" ? name : null;
}
