// Deep-link support for the shell: ``?open_app=<name>`` on the shell origin
// asks this client to open (or focus) that app's tab after the initial layout
// mounts. The desktop client's cross-workspace app selector builds such URLs
// (riding its ``/goto/<host-id>/`` cookie bridge); the parameter is consumed
// exactly once and stripped from the address bar so a reload does not re-open
// the tab. Pure helpers only -- the dockview side lives in
// ``DockviewWorkspace.ts`` (``consumeOpenAppDeepLink``).

/** The requested app name in ``search`` (a ``location.search`` string), or null. */
export function openAppNameFromSearch(search: string): string | null {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const name = params.get("open_app");
  return name !== null && name !== "" ? name : null;
}

/** ``search`` minus the ``open_app`` parameter, preserving everything else. */
export function searchWithoutOpenApp(search: string): string {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  params.delete("open_app");
  const rest = params.toString();
  return rest === "" ? "" : `?${rest}`;
}
