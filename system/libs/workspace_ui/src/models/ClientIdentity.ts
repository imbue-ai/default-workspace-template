/**
 * Per-browser client identity.
 *
 * Each browser gets a stable uuid (minted once, kept in localStorage). The active desktop is
 * module state only: the shell's client record is its source (desktop-interface contracts.md
 * section 4.3), read on boot, so two windows of one browser land on the same desktop. The
 * identity travels with every chat message and with the WebSocket `client_state` registration,
 * so the server (and agents, via `layout.py context`) can attribute requests to a client and
 * its desktop.
 */

const CLIENT_ID_STORAGE_KEY = "si-client-id";

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

// The active desktop id, while the page lives. Empty string means "not chosen yet" (during
// startup, before the client record and the desktops have been fetched).
let activeDesktopId = "";

export function getActiveDesktopId(): string {
  return activeDesktopId;
}

export function setActiveDesktopId(desktopId: string): void {
  activeDesktopId = desktopId;
}

export interface AdoptedClientIdentity {
  clientId: string;
  desktopId: string;
}

/**
 * Take on the identity the shell handed this page in its handshake (an app page framed by
 * the shell). The chat document runs on its own origin with its own local storage, so
 * minting an id of its own would make one browser two clients; the shell's id and desktop
 * are the truth. Nothing is written to storage: the identity is the shell's to keep.
 */
export function adoptClientIdentity(identity: AdoptedClientIdentity): void {
  cachedClientId = identity.clientId;
  activeDesktopId = identity.desktopId;
}
