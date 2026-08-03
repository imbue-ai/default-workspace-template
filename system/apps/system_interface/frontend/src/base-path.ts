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

let cachedHostname: string | null = null;

export function getHostname(): string {
  if (cachedHostname !== null) {
    return cachedHostname;
  }
  const metaElement = document.querySelector('meta[name="system-interface-hostname"]');
  cachedHostname = metaElement?.getAttribute("content") ?? "localhost";
  return cachedHostname;
}

let cachedPrimaryAgentId: string | null = null;

export function getPrimaryAgentId(): string {
  if (cachedPrimaryAgentId !== null) {
    return cachedPrimaryAgentId;
  }
  const metaElement = document.querySelector('meta[name="system-interface-agent-id"]');
  cachedPrimaryAgentId = metaElement?.getAttribute("content") ?? "";
  return cachedPrimaryAgentId;
}

/** Split the meta tag's comma-separated service-name list into a lookup set.
 *  Tolerates surrounding whitespace and an empty/absent value (empty set). */
export function parseSelfReferentialServices(rawValue: string): ReadonlySet<string> {
  return new Set(
    rawValue
      .split(",")
      .map((name) => name.trim())
      .filter((name) => name.length > 0),
  );
}

let cachedSelfReferentialServices: ReadonlySet<string> | null = null;

/**
 * Service names that resolve back to the instance serving THIS shell, so
 * framing one would nest the shell inside itself.
 *
 * Every service owns a browser origin derived client-side (see ``origin.ts``),
 * so the browser loads it directly and the shell's backend never sees the
 * request -- the refusal has to happen here, at the panel, before the iframe
 * is created.
 *
 * Empty for the workspace's own system interface: it is not registered as a
 * service, so no layout can point a panel back at it. The live-editing preview
 * sets both of its own service names, because the preview tab stays open for
 * the whole editing pass and is therefore present in the live layout the
 * preview copies -- rendering it would frame the wrapper that frames the
 * preview, and every nested iframe would load another full system interface.
 */
export function getSelfReferentialServices(): ReadonlySet<string> {
  if (cachedSelfReferentialServices !== null) {
    return cachedSelfReferentialServices;
  }
  const metaElement = document.querySelector('meta[name="system-interface-self-referential-services"]');
  cachedSelfReferentialServices = parseSelfReferentialServices(metaElement?.getAttribute("content") ?? "");
  return cachedSelfReferentialServices;
}
