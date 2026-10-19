/**
 * The words for a chat that is switching: what the held bubbles' caption, the activity strip,
 * and the composer's placeholder say in each phase of a handoff (spec 5.1) or a rebind (spec 6).
 */

import type { HandoffState } from "../models/Chats";
import { harnessLabel } from "./harness-labels";

/** The phase, as the page tells it. ``retiringHarness`` is the active agent's: the one the chat
 *  is leaving in a handoff, the one being restarted in a rebind. */
export function handoffPhaseText(handoff: HandoffState, retiringHarness: string): string {
  const from = harnessLabel(retiringHarness);
  // A handoff names the harness it moves to; a rebind keeps the harness and names the account.
  const to = handoff.kind === "rebind" ? `${from} on ${handoff.target_label}` : harnessLabel(handoff.target_harness);
  switch (handoff.phase) {
    case "draining":
      return `Wrapping up with ${from}…`;
    case "summarizing":
      return `${from} is writing a summary…`;
    case "switching":
      return `Starting ${to}…`;
    case "restarting":
      return `Restarting ${to}…`;
    case "failed":
      return handoff.kind === "rebind" ? `Could not restart ${to}` : `Could not start ${to}`;
    default:
      return handoff.phase satisfies never;
  }
}

/** The composer's placeholder while the chat switches: a message typed now is held until the agent is ready. */
export function handoffComposerPlaceholder(handoff: HandoffState): string {
  const destination = handoff.kind === "rebind" ? handoff.target_label : harnessLabel(handoff.target_harness);
  return `Type a message; it is delivered once ${destination} is ready…`;
}
