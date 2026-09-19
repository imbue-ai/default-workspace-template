/**
 * Who is here: the shell's presence heartbeat and the connected users the WebSocket pushes.
 *
 * Every page load mints a session id and heartbeats `/api/presence/heartbeat` every
 * `HEARTBEAT_INTERVAL_MS` while the tab is visible (once immediately when it becomes
 * visible), and beacons `/api/presence/leave` on `pagehide`. The proxy in front of the shell
 * stamps the request's identity, so the page sends nothing about who it is; the heartbeat's
 * answer is the requester's own identity record (or 204 when the workspace carries none), which
 * is what the account affordances render. The connected set itself arrives as
 * `presence_updated` over the shell's WebSocket, one entry per user.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export const HEARTBEAT_INTERVAL_MS = 30_000;

/** One connected user as the shell serializes it (one entry per user, however many tabs). */
export interface PresentUser {
  user_id: string;
  email: string;
  display_name: string | null;
  avatar_url: string | null;
  owner: boolean;
  session_count: number;
  first_seen: string;
  last_seen: string;
}

/** The requester's own identity, as the heartbeat answers it. */
export interface OwnIdentity {
  owner: boolean;
  user_id: string;
  email: string;
  display_name?: string;
  avatar_url?: string;
}

let presentUsers: PresentUser[] = [];
let ownIdentity: OwnIdentity | null = null;
let sessionId: string | null = null;
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
let isStarted = false;

export function getPresentUsers(): PresentUser[] {
  return presentUsers;
}

export function getOwnIdentity(): OwnIdentity | null {
  return ownIdentity;
}

/** Apply a `presence_updated` push: the whole connected set, replacing the last one. */
export function applyPresence(users: PresentUser[]): void {
  presentUsers = users;
}

function mintSessionId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `session-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function getSessionId(): string {
  if (sessionId === null) sessionId = mintSessionId();
  return sessionId;
}

function sessionBody(): string {
  return JSON.stringify({ session_id: getSessionId() });
}

/** Whether the shell is reached over a share (any host that is not a local forward origin). */
export function isSharedHost(host: string): boolean {
  const hostname = host.replace(/:\d+$/, "").toLowerCase();
  return !(hostname === "localhost" || hostname.endsWith(".localhost") || hostname === "127.0.0.1");
}

/** The identity refresh the gateway serves at every shared origin; null on a local forward, where there is nothing to refresh. */
export function identityRefreshUrl(
  location: Pick<Location, "host" | "origin" | "href"> = window.location,
): string | null {
  if (!isSharedHost(location.host)) return null;
  return `${location.origin}/_auth/refresh?next=${encodeURIComponent(location.href)}`;
}

async function sendHeartbeat(): Promise<void> {
  let response: Response;
  try {
    response = await fetch(apiUrl("/api/presence/heartbeat"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: sessionBody(),
      keepalive: true,
    });
  } catch (e) {
    console.warn("[si-presence] heartbeat failed", e);
    return;
  }
  if (response.status === 204) {
    ownIdentity = null;
    return;
  }
  if (!response.ok) {
    console.warn(`[si-presence] heartbeat answered HTTP ${response.status}`);
    return;
  }
  const body = (await response.json()) as { identity?: OwnIdentity };
  ownIdentity = body.identity ?? null;
  m.redraw();
}

function sendLeave(): void {
  const body = new Blob([sessionBody()], { type: "application/json" });
  navigator.sendBeacon(apiUrl("/api/presence/leave"), body);
}

function startTimer(): void {
  if (heartbeatTimer !== null) return;
  heartbeatTimer = setInterval(() => {
    void sendHeartbeat();
  }, HEARTBEAT_INTERVAL_MS);
}

function stopTimer(): void {
  if (heartbeatTimer === null) return;
  clearInterval(heartbeatTimer);
  heartbeatTimer = null;
}

function handleVisibilityChange(): void {
  if (document.visibilityState === "visible") {
    void sendHeartbeat();
    startTimer();
  } else {
    stopTimer();
  }
}

/** Start heartbeating for this page. Called once when the shell boots; safe to call again. */
export function startPresenceHeartbeat(): void {
  if (isStarted) return;
  isStarted = true;
  document.addEventListener("visibilitychange", handleVisibilityChange);
  window.addEventListener("pagehide", sendLeave);
  handleVisibilityChange();
}

/** Stop heartbeating and forget everything, so the next start binds to the current `window`. Test-only. */
export function resetPresenceForTesting(): void {
  if (isStarted) {
    document.removeEventListener("visibilitychange", handleVisibilityChange);
    window.removeEventListener("pagehide", sendLeave);
  }
  stopTimer();
  isStarted = false;
  sessionId = null;
  ownIdentity = null;
  presentUsers = [];
}
