/**
 * Reads the application base path from a <meta> tag injected by the backend.
 * When running behind a reverse proxy with a path prefix (e.g. /myapp), the
 * backend sets <meta name="system-interface-base-path" content="/myapp"> so that
 * the frontend can build correct URLs and route prefixes.
 *
 * The returned value never has a trailing slash. For an app served at the
 * domain root, it returns "".
 */

let cachedBasePath: string | null = null;

export function getBasePath(): string {
  if (cachedBasePath !== null) {
    return cachedBasePath;
  }
  const metaElement = document.querySelector('meta[name="system-interface-base-path"]');
  const rawValue = metaElement?.getAttribute("content") ?? "";
  cachedBasePath = rawValue.replace(/\/+$/, "");
  return cachedBasePath;
}

export function apiUrl(path: string): string {
  return getBasePath() + path;
}

/** The WebSocket URL for an API path: ``apiUrl`` with the page's ws(s) scheme and host
 *  (``location`` is the page's own; a test passes another). */
export function wsUrl(path: string, location: Pick<Location, "protocol" | "host"> = window.location): string {
  const base = apiUrl(path);
  const loc = location;
  const protocol = loc.protocol === "https:" ? "wss:" : "ws:";
  if (base.startsWith("http")) {
    return base.replace(/^http/, "ws");
  }
  return `${protocol}//${loc.host}${base}`;
}
