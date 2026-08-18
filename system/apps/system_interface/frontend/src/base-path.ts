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

// Feature flags arrive as meta tags the server injects from its environment (see
// server.py's _FEATURE_FLAG_META_TAGS). Read once per tag and cached, since the tag
// cannot change without a page load.
const cachedFeatureFlags = new Map<string, boolean>();

function isFeatureFlagEnabled(metaTagName: string): boolean {
  const cached = cachedFeatureFlags.get(metaTagName);
  if (cached !== undefined) {
    return cached;
  }
  const metaElement = document.querySelector(`meta[name="${metaTagName}"]`);
  // Off unless the tag is explicitly "true", so a server that never injected it
  // (or an older one) leaves the gated surface hidden.
  const isEnabled = metaElement?.getAttribute("content") === "true";
  cachedFeatureFlags.set(metaTagName, isEnabled);
  return isEnabled;
}

/**
 * Whether the non-claude harnesses are enabled for this host (backend
 * FEATURE_FLAG_ENABLE_OTHER_HARNESSES). Gates the new-tab menu's
 * "New <harness> agent" launchers (Codex, Pi) and nothing else -- an existing codex or
 * pi agent stays fully functional with the flag off, model bar included, since it may
 * have been created outside this menu.
 */
export function areOtherHarnessesEnabled(): boolean {
  return isFeatureFlagEnabled("system-interface-enable-other-harnesses");
}

/**
 * Whether the "New introductory <harness> chat" launchers are enabled for this host
 * (backend FEATURE_FLAG_ENABLE_INTRODUCTORY_AGENTS_IN_OTHER_HARNESSES). They create a
 * chat with the `first` create template stacked -- fast launch where the harness
 * supports it, /welcome, the first=true label -- so the introductory-chat flow can be
 * exercised without re-creating a workspace. Independent of the flag above: an
 * introductory chat is a normal chat, so gating it separately keeps "which harnesses
 * can be launched" and "can I make another introductory chat" from moving together.
 */
export function areIntroductoryAgentsEnabled(): boolean {
  return isFeatureFlagEnabled("system-interface-enable-introductory-agents");
}
