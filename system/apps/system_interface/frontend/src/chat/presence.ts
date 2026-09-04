/**
 * The chat page's presence reports (see `presence.py`): `hidden` once the shell has handed
 * the page its handshake, `visible` on `shell:shown`, `hidden` on `shell:hidden`, `closed` on
 * `pagehide`, and a heartbeat of the current state every minute so a page that vanished
 * without its `pagehide` stops counting on its own. The OOM prioritizer reads the aggregate.
 */

import { apiUrl } from "../base-path";

export type PresenceState = "visible" | "hidden" | "closed";

// Matches PRESENCE_HEARTBEAT_SECONDS in presence.py.
const HEARTBEAT_MS = 60_000;

let heartbeat: ReturnType<typeof setInterval> | null = null;
let currentState: PresenceState = "hidden";
let reportingAgentId: string | null = null;
let reportingClientId: string | null = null;

function post(agentId: string, clientId: string, state: PresenceState): void {
  // keepalive lets the closed report leave with the page on pagehide.
  void fetch(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/presence`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: clientId, state }),
    keepalive: true,
  }).catch(() => {
    // Best-effort: the next heartbeat corrects a dropped report.
  });
}

/** Start reporting for this page's chat as `clientId`; a second call re-keys the reports. */
export function startPresenceReporting(agentId: string, clientId: string, initialState: PresenceState): void {
  reportingAgentId = agentId;
  reportingClientId = clientId;
  currentState = initialState;
  post(agentId, clientId, initialState);
  if (heartbeat === null) {
    heartbeat = setInterval(() => {
      if (reportingAgentId !== null && reportingClientId !== null && currentState !== "closed") {
        post(reportingAgentId, reportingClientId, currentState);
      }
    }, HEARTBEAT_MS);
  }
}

/** Report a change of state; a no-op until reporting has started. */
export function reportPresence(state: PresenceState): void {
  currentState = state;
  if (reportingAgentId === null || reportingClientId === null) return;
  post(reportingAgentId, reportingClientId, state);
}

export function currentPresenceState(): PresenceState {
  return currentState;
}
