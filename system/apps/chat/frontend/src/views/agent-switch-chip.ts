/**
 * The user-facing names of the harnesses (`HarnessType` on the backend), and the words for the
 * seam between two agents of one chat, which the handoff node renders (``handoff-node.ts``).
 */

import type { AgentSwitchEvent } from "../models/Response";

// An unknown harness shows its raw name rather than nothing.
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
