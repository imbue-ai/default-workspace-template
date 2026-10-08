/**
 * The chat page's presence reports (see `presence.py`): `hidden` once the shell has handed
 * the page its handshake, `visible` on `shell:shown`, `hidden` on `shell:hidden`, `closed` on
 * `pagehide`, whether the document has focus on every window `focus` and `blur`, and a
 * heartbeat of all of it every thirty seconds, so a page that vanished without its `pagehide`
 * stops counting on its own. Every report names this page load's instance id, so two pages of
 * the chat in one client never overwrite each other's report, and its place in the order this
 * page sent them, since the chat app can receive two reports sent a moment apart in either
 * order. Only the chat's own page
 * reports, never a subagent view. The OOM prioritizer reads the aggregate, and the notify
 * path asks which pages are watching (shown and focused).
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export type PresenceState = "visible" | "hidden" | "closed";

// Matches WATCH_HEARTBEAT_SECONDS in presence.py.
const HEARTBEAT_MS = 30_000;

// One per page load: what the chat app keys this page's reports on.
const instanceId: string =
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `page-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;

let lastSequence = 0;
let heartbeat: ReturnType<typeof setInterval> | null = null;
let currentState: PresenceState = "hidden";
let reportingChatId: string | null = null;
let reportingClientId: string | null = null;

function post(chatId: string, clientId: string, state: PresenceState): void {
  // keepalive lets the closed report leave with the page on pagehide.
  void fetch(apiUrl(`/api/chats/${encodeURIComponent(chatId)}/presence`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      instance_id: instanceId,
      client_id: clientId,
      state,
      is_focused: document.hasFocus(),
      sequence: ++lastSequence,
    }),
    keepalive: true,
  }).catch(() => {
    // Best-effort: the next heartbeat corrects a dropped report.
  });
}

function postCurrent(): void {
  if (reportingChatId === null || reportingClientId === null || currentState === "closed") return;
  post(reportingChatId, reportingClientId, currentState);
}

/** Start reporting for this page's chat in client `clientId`; a second call re-keys the client. */
export function startPresenceReporting(chatId: string, clientId: string, initialState: PresenceState): void {
  reportingChatId = chatId;
  reportingClientId = clientId;
  currentState = initialState;
  post(chatId, clientId, initialState);
  if (heartbeat === null) heartbeat = setInterval(postCurrent, HEARTBEAT_MS);
}

/** Report a change of state; a no-op until reporting has started. */
export function reportPresence(state: PresenceState): void {
  currentState = state;
  if (reportingChatId === null || reportingClientId === null) return;
  post(reportingChatId, reportingClientId, state);
}

/** Report that the document gained or lost focus; a no-op until reporting has started, and after closed. */
export function reportFocusChange(): void {
  postCurrent();
}

export function currentPresenceState(): PresenceState {
  return currentState;
}
