/**
 * Who is here: the shell's presence heartbeat and the connected users the WebSocket pushes.
 *
 * The page posts `/api/presence/heartbeat` (no body) every `HEARTBEAT_INTERVAL_MS` while the tab
 * is visible, once immediately when it becomes visible, and nothing when it goes away: the shell
 * counts a user as gone once their heartbeats stop. The proxy in front of the shell stamps the
 * request's identity, so the page sends nothing about who it is; the heartbeat's answer is the
 * requester's own identity record (or 204 when the workspace carries none), which is how the
 * Presence widget tells the viewer's own entry from the rest. The connected set itself arrives as
 * `presence_updated` over the shell's WebSocket, one entry per user, each with the name and
 * profile picture imbue_cloud holds for them.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import type { PresentUser } from "./records";

export const HEARTBEAT_INTERVAL_MS = 30_000;

/** The requester's own identity, as the heartbeat answers it: who they are, not what they are called. */
export interface OwnIdentity {
  owner: boolean;
  user_id: string;
  email: string;
}

let presentUsers: PresentUser[] = [];
let ownIdentity: OwnIdentity | null = null;
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

async function sendHeartbeat(): Promise<void> {
  let response: Response;
  try {
    response = await fetch(apiUrl("/api/presence/heartbeat"), { method: "POST", keepalive: true });
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
  handleVisibilityChange();
}

/** Make the page's own identity what a heartbeat would have answered, without one. Test-only. */
export function setOwnIdentityForTesting(identity: OwnIdentity | null): void {
  ownIdentity = identity;
}

/** Stop heartbeating and forget everything, so the next start binds to the current `window`. Test-only. */
export function resetPresenceForTesting(): void {
  if (isStarted) {
    document.removeEventListener("visibilitychange", handleVisibilityChange);
  }
  stopTimer();
  isStarted = false;
  ownIdentity = null;
  presentUsers = [];
}
