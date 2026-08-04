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

let cachedSillyModelsEnabled: boolean | null = null;

export function isSillyModelsEnabled(): boolean {
  if (cachedSillyModelsEnabled !== null) {
    return cachedSillyModelsEnabled;
  }
  const metaElement = document.querySelector('meta[name="system-interface-enable-silly-models"]');
  cachedSillyModelsEnabled = metaElement?.getAttribute("content") === "true";
  return cachedSillyModelsEnabled;
}

let cachedCodexEnabled: boolean | null = null;

/**
 * Whether the codex harness is enabled for this host (backend FEATURE_FLAG_ENABLE_CODEX,
 * delivered as the ``system-interface-enable-codex`` meta tag). Gates the "New Codex
 * Agent" launcher. Off unless the meta tag is explicitly "true".
 */
export function isCodexEnabled(): boolean {
  if (cachedCodexEnabled !== null) {
    return cachedCodexEnabled;
  }
  const metaElement = document.querySelector('meta[name="system-interface-enable-codex"]');
  cachedCodexEnabled = metaElement?.getAttribute("content") === "true";
  return cachedCodexEnabled;
}
