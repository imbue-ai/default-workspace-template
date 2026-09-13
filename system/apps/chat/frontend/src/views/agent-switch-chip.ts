/**
 * The chip between two agents' segments of one chat: "Switched from Claude to Codex".
 *
 * The backend synthesizes one `agent_switch` event per handoff (`chat_transcript.py`); this
 * renders it as a centered, non-interactive pill so the seam reads as part of the
 * conversation rather than as a message from either side.
 */

import m from "mithril";
import type { AgentSwitchEvent } from "../models/Response";

// The user-facing name of each harness (`HarnessType` on the backend). An unknown harness
// shows its raw name rather than nothing.
const HARNESS_LABEL_BY_NAME: Record<string, string> = {
  claude: "Claude",
  codex: "Codex",
  "pi-coding": "Pi",
  opencode: "OpenCode",
  antigravity: "Antigravity",
};

export function harnessLabel(harness: string): string {
  return HARNESS_LABEL_BY_NAME[harness] ?? harness;
}

export function agentSwitchText(event: AgentSwitchEvent): string {
  return `Switched from ${harnessLabel(event.from_harness)} to ${harnessLabel(event.to_harness)}`;
}

/** The chip row. Its root carries the event id as its DOM id, like every other row, so the
 *  virtualized list can measure it. */
export function renderAgentSwitchChip(event: AgentSwitchEvent): m.Vnode {
  return m(
    "div",
    { id: event.event_id, key: event.event_id, class: "message message-agent-switch mb-3 flex justify-center" },
    [
      m(
        "span",
        {
          class: "rounded-full border border-default px-3 py-1 text-(length:--font-size-helper) text-secondary",
          title: `${event.from_agent_id} to ${event.to_agent_id}`,
        },
        agentSwitchText(event),
      ),
    ],
  );
}
