/**
 * Per-browser client identity.
 *
 * Each browser gets a stable uuid (minted once, kept in localStorage), a
 * device kind derived from the user agent (mobile vs desktop), and an active
 * view id (also persisted per browser so reconnects restore the same view).
 * The identity travels with every chat message and with the WebSocket
 * `client_state` registration, so the server (and agents, via
 * `layout.py context`) can attribute requests to a client and its view.
 */

const CLIENT_ID_STORAGE_KEY = "si-client-id";
const ACTIVE_PROJECT_STORAGE_KEY = "si-active-project-id";

export type DeviceKind = "mobile" | "desktop";

/** Pure UA classifier, separated from the navigator read for unit testing. */
export function classifyDeviceKind(userAgentDataMobile: boolean | undefined, userAgent: string): DeviceKind {
  if (userAgentDataMobile !== undefined) {
    return userAgentDataMobile ? "mobile" : "desktop";
  }
  return /Mobi|Android|iPhone|iPad|iPod/i.test(userAgent) ? "mobile" : "desktop";
}

export function getDeviceKind(): DeviceKind {
  if (adoptedDeviceKind !== null) return adoptedDeviceKind;
  // navigator.userAgentData is Chromium-only, hence the UA-string fallback.
  const uaData = (navigator as { userAgentData?: { mobile?: boolean } }).userAgentData;
  return classifyDeviceKind(uaData?.mobile, navigator.userAgent);
}

let cachedClientId: string | null = null;

export function getClientId(): string {
  if (cachedClientId !== null) {
    return cachedClientId;
  }
  const stored = localStorage.getItem(CLIENT_ID_STORAGE_KEY);
  if (stored) {
    cachedClientId = stored;
    return stored;
  }
  const minted =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `client-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  localStorage.setItem(CLIENT_ID_STORAGE_KEY, minted);
  cachedClientId = minted;
  return minted;
}

// The active view id (a project id, or Everything). Held in module state
// (source of truth while the page lives) and mirrored to localStorage so the
// same browser restores the same view on its next connect. Empty string means
// "not chosen yet" (during startup, before the projects list has been fetched).
let activeProjectId = "";

export function getStoredProjectId(): string {
  return localStorage.getItem(ACTIVE_PROJECT_STORAGE_KEY) ?? "";
}

export function getActiveProjectId(): string {
  return activeProjectId;
}

export function setActiveProjectId(projectId: string): void {
  activeProjectId = projectId;
  localStorage.setItem(ACTIVE_PROJECT_STORAGE_KEY, projectId);
}

export interface AdoptedClientIdentity {
  clientId: string;
  deviceKind: DeviceKind | string;
  viewId: string;
}

// The device kind the shell handed over, when it did; the UA classification otherwise.
let adoptedDeviceKind: DeviceKind | null = null;

/**
 * Take on the identity the shell handed this page in its handshake (an app page framed by
 * the shell). The chat document runs on its own origin with its own local storage, so
 * minting an id of its own would make one browser two clients; the shell's id and view
 * are the truth. Nothing is written to storage: the identity is the shell's to keep.
 */
export function adoptClientIdentity(identity: AdoptedClientIdentity): void {
  cachedClientId = identity.clientId;
  adoptedDeviceKind = identity.deviceKind === "mobile" ? "mobile" : "desktop";
  activeProjectId = identity.viewId;
}
