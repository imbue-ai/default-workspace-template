/**
 * The words for a chat that is switching harness: what the held bubbles' caption, the activity
 * strip, and the composer's placeholder say in each phase of the handoff (spec 5.1).
 */

import type { HandoffState } from "../models/Chats";
import { harnessLabel } from "./agent-switch-chip";

/** The phase, as the page tells it. ``retiringHarness`` is the active agent's while the chat is
 *  still read from the agent it is leaving. */
export function handoffPhaseText(handoff: HandoffState, retiringHarness: string): string {
  const from = harnessLabel(retiringHarness);
  const to = harnessLabel(handoff.target_harness);
  switch (handoff.phase) {
    case "draining":
      return `Wrapping up with ${from}…`;
    case "summarizing":
      return `${from} is writing a summary…`;
    case "switching":
      return `Starting ${to}…`;
    case "failed":
      return `Could not start ${to}`;
    default:
      return handoff.phase satisfies never;
  }
}

/** Whether the switch can still be called off: only until the old agent is stopped (spec 5.6). */
export function isHandoffCancellable(handoff: HandoffState): boolean {
  return handoff.phase === "draining" || handoff.phase === "summarizing";
}

/** The composer's placeholder while the chat switches: a message typed now is held for the new agent. */
export function handoffComposerPlaceholder(handoff: HandoffState): string {
  return `Type a message; it is delivered once ${harnessLabel(handoff.target_harness)} is ready…`;
}
