/**
 * Who is here: the shell's presence heartbeat and the connected users the WebSocket pushes.
 *
 * The page posts `/api/presence/heartbeat` every `HEARTBEAT_INTERVAL_MS` while the tab is
 * visible, once immediately when it becomes visible, and nothing when it goes away: the shell
 * counts a user as gone once their heartbeats stop. The body carries how long ago the page last
 * saw keyboard or pointer input (`input_age_seconds`), never a moment by this clock, so a clock
 * ahead of or behind the shell's cannot pin anyone active or idle; the shell stamps the moment
 * itself, which is what tells an active person from an idle one. The proxy in front of the shell
 * stamps the request's identity, so the page sends nothing
 * about who it is; the heartbeat's answer is the requester's own identity record and participant
 * key (the owner of an unshared workspace is `owner`; a visiting agent gets 204), which is how the
 * tray tells the viewer's own entries from the rest. The connected set itself arrives as
 * `presence_updated` over the shell's WebSocket, one entry per account, each with the name and
 * profile picture imbue_cloud holds for them.
 *
 * The heartbeat also carries which windows a person interacted with since the last answered
 * heartbeat (`client_id` and `input_windows`, the app-usage-signals spec section 6.3): the page
 * driver reports each `shell:input` with its window and app, and focus landing in a page that binds
 * no contract, and the shell writes one `window_input` per client, window and minute. The set is
 * cleared once a heartbeat is answered and kept across a failed send, so nothing is lost to an
 * outage; input outranks focus for a window reported with both.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import type { PresentUser } from "./records";

export const HEARTBEAT_INTERVAL_MS = 30_000;

/** The requester's own identity, as the heartbeat answers it: who they are, not what they are called. On an
 *  unshared workspace the owner has no account, so `user_id` and `email` are null and the key is `owner`. */
export interface OwnIdentity {
  owner: boolean;
  user_id: string | null;
  email: string | null;
  /** The participant key the page's own entries carry (the multiplayer spec, section 4.1). */
  participant: string;
}

/** What a page's report says a person did in a window: the page's own input report, or focus landing in a page
 *  that reports none (the app-usage-signals spec, section 6.4). */
export type PageInputKind = "input" | "focus";

/** One window the next heartbeat reports, as the shell reads it. */
export interface WindowInputReport {
  window_id: string;
  app: string;
  kind: PageInputKind;
}

/** The input events whose newest occurrence the heartbeat reports. */
const INPUT_EVENT_TYPES: readonly string[] = ["pointerdown", "keydown"];

let presentUsers: PresentUser[] = [];
let ownIdentity: OwnIdentity | null = null;
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
let isStarted = false;
let lastInputAt: Date | null = null;
let clientId: string | null = null;
/** The windows reported since the last answered heartbeat, by window id; input outranks focus. */
let inputWindows = new Map<string, { app: string; kind: PageInputKind }>();
const identityListeners = new Set<() => void>();

/** Hear when a heartbeat changes who this page is (the first answer, or a new account): the viewing chrome and
 *  the viewed layout both read the identity, and until the first answer the page is nobody. */
export function onOwnIdentityChanged(listener: () => void): void {
  identityListeners.add(listener);
}

export function getPresentUsers(): PresentUser[] {
  return presentUsers;
}

export function getOwnIdentity(): OwnIdentity | null {
  return ownIdentity;
}

/** The page's own participant key, once a heartbeat has answered. */
export function getOwnParticipant(): string | null {
  return ownIdentity?.participant ?? null;
}

function noteInput(): void {
  lastInputAt = new Date();
}

/** Count input a page of this client reported (``shell:input``): typing and clicking happen inside the app
 *  frames, whose events never bubble to this window, so the pages say so themselves (the multiplayer spec,
 *  section 4.3), and remember the window for the next heartbeat (the app-usage-signals spec, section 6.3).
 *  Focus landing in a page that reports none is remembered the same way but says nothing about the person
 *  being active: the shell may have put the window on screen itself. */
export function notePageInput(windowId: string, app: string, kind: PageInputKind): void {
  if (kind === "input") noteInput();
  rememberInputWindow(windowId, { app, kind });
}

function rememberInputWindow(windowId: string, report: { app: string; kind: PageInputKind }): void {
  const known = inputWindows.get(windowId);
  if (known === undefined || known.kind === "focus") inputWindows.set(windowId, report);
}

/** The windows the next heartbeat will report, in the order they were first noted. Test-only. */
export function pendingInputReportsForTesting(): WindowInputReport[] {
  return [...inputWindows].map(([window_id, { app, kind }]) => ({ window_id, app, kind }));
}

function parseOwnIdentity(body: unknown): OwnIdentity | null {
  if (body === null || typeof body !== "object") return null;
  const record = body as { identity?: unknown; participant?: unknown };
  if (typeof record.participant !== "string" || record.identity === null || typeof record.identity !== "object") {
    return null;
  }
  const identity = record.identity as { owner?: unknown; user_id?: unknown; email?: unknown };
  return {
    owner: identity.owner === true,
    user_id: typeof identity.user_id === "string" ? identity.user_id : null,
    email: typeof identity.email === "string" ? identity.email : null,
    participant: record.participant,
  };
}

/** Apply a `presence_updated` push: the whole connected set, replacing the last one. */
export function applyPresence(users: PresentUser[]): void {
  presentUsers = users;
}

/** How long ago this page last saw input, in seconds, as the heartbeat reports it; null for none. */
function inputAgeSeconds(): number | null {
  if (lastInputAt === null) return null;
  return Math.max(0, (Date.now() - lastInputAt.getTime()) / 1000);
}

async function sendHeartbeat(): Promise<void> {
  let response: Response;
  // The reports leave with this heartbeat and come back only if it fails: a report noted meanwhile is the next
  // heartbeat's, and a window reported both times keeps the stronger kind.
  const reported = inputWindows;
  inputWindows = new Map();
  const body = {
    input_age_seconds: inputAgeSeconds(),
    client_id: clientId,
    input_windows: [...reported].map(([window_id, { app, kind }]) => ({ window_id, app, kind })),
  };
  try {
    response = await fetch(apiUrl("/api/presence/heartbeat"), {
      method: "POST",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    console.warn("[si-presence] heartbeat failed", e);
    keepReports(reported);
    return;
  }
  if (response.status === 204) {
    ownIdentity = null;
    return;
  }
  if (!response.ok) {
    console.warn(`[si-presence] heartbeat answered HTTP ${response.status}`);
    keepReports(reported);
    return;
  }
  const before = ownIdentity?.participant ?? null;
  ownIdentity = parseOwnIdentity(await response.json());
  if ((ownIdentity?.participant ?? null) !== before) {
    for (const listener of identityListeners) listener();
  }
  m.redraw();
}

/** Put the reports of a heartbeat that did not get through back for the next one. */
function keepReports(reported: ReadonlyMap<string, { app: string; kind: PageInputKind }>): void {
  for (const [windowId, report] of reported) rememberInputWindow(windowId, report);
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

/** Start heartbeating for this page as ``pageClientId``. Called once when the shell boots; safe to call again. */
export function startPresenceHeartbeat(pageClientId: string): void {
  if (isStarted) return;
  isStarted = true;
  clientId = pageClientId;
  document.addEventListener("visibilitychange", handleVisibilityChange);
  for (const type of INPUT_EVENT_TYPES) window.addEventListener(type, noteInput, { capture: true, passive: true });
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
    for (const type of INPUT_EVENT_TYPES) window.removeEventListener(type, noteInput, { capture: true });
  }
  stopTimer();
  isStarted = false;
  ownIdentity = null;
  presentUsers = [];
  lastInputAt = null;
  clientId = null;
  inputWindows = new Map();
  identityListeners.clear();
}
