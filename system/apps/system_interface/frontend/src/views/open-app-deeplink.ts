// ``?open_app=<name>`` deep-link parsing. Pure helpers only -- the dockview side
// lives in ``DockviewWorkspace.ts`` (``consumeOpenAppDeepLink``).

/** The requested app name in ``query`` (a URL query string, leading ``?``
 *  optional), or null. "Query" as in the URL component, not searching. */
export function openAppNameFromQuery(query: string): string | null {
  const params = new URLSearchParams(query.startsWith("?") ? query.slice(1) : query);
  const name = params.get("open_app");
  return name !== null && name !== "" ? name : null;
}
