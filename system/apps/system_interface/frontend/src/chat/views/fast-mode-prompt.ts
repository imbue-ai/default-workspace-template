/**
 * When a conversation has used up its fast-mode grace period.
 *
 * The workspace's first chat runs fast so it feels responsive, then asks
 * whether to keep paying for that (see models/FastModePrompt.ts). This module
 * decides when "then" is: it counts the conversation's completed user turns and
 * reports whether the prompt is now owed.
 *
 * The prompt is agent-scoped and harness-declared: it fires only for an agent
 * whose harness declared the `fast_mode_prompt` turn check (claude, codex),
 * that carries the `first=true` label (the one chat the `first` template
 * launched fast), and that has not been asked before.
 *
 * A turn is counted exactly as the transcript view counts one, by reusing the
 * same boundary rule the timeline groups on -- so "5 turns" means the five
 * exchanges the user can actually see, not five raw transcript lines. Permission
 * verdicts are excluded on top of that: the timeline treats them as turn
 * boundaries (see buildSections) but they are the app talking to itself, not the
 * user taking another turn.
 */

import type { TranscriptEvent } from "../models/Response";
import type { AgentState } from "../../models/AgentManager";
import { getAgentFastMode } from "../models/ModelSettings";
import { hasFastModePrompt } from "../models/HarnessCatalog";
import { isFastModePromptAnswered, openFastModePrompt } from "../models/FastModePrompt";
import { isNonBoundaryUserMessage, resolutionOf } from "./message-classification";

/** How many user turns the first chat runs with fast mode on before it asks
 *  whether to keep it. The one knob for the grace period. */
export const FAST_MODE_GRACE_TURN_COUNT = 5;

/** How many turns the user has actually taken in this conversation. */
export function countUserTurns(events: TranscriptEvent[]): number {
  let count = 0;
  for (const event of events) {
    if (event.type !== "user_message") {
      continue;
    }
    if (isNonBoundaryUserMessage(event)) {
      continue;
    }
    if (resolutionOf(event) !== null) {
      continue;
    }
    count = count + 1;
  }
  return count;
}

/**
 * Whether this conversation now owes the user the fast-mode prompt.
 *
 * Requires the agent to be idle so the prompt lands between turns rather than
 * interrupting a reply, and requires fast mode to still be on -- a user who
 * already turned it off with the composer toggle has answered the question the
 * prompt would ask (and a harness that refused the fast launch reads as off in
 * its model state, so no prompt is owed for a speed nobody got).
 */
export function isFastModePromptOwed(
  agent: AgentState | undefined,
  events: TranscriptEvent[],
  isAgentIdle: boolean,
): boolean {
  if (agent === undefined || !hasFastModePrompt(agent.harness)) {
    return false;
  }
  if (agent.labels["first"] !== "true") {
    return false;
  }
  if (isFastModePromptAnswered(agent.id, agent.labels)) {
    return false;
  }
  if (!isAgentIdle) {
    return false;
  }
  if (!getAgentFastMode(agent.id)) {
    return false;
  }
  return countUserTurns(events) >= FAST_MODE_GRACE_TURN_COUNT;
}

/** Raise the prompt if this conversation has earned it. Safe to call on every
 *  render: opening is idempotent, and the gates that walk the transcript sit
 *  behind the cheap ones (see isFastModePromptOwed). */
export function maybePromptForFastMode(
  agent: AgentState | undefined,
  events: TranscriptEvent[],
  isAgentIdle: boolean,
): void {
  if (agent !== undefined && isFastModePromptOwed(agent, events, isAgentIdle)) {
    openFastModePrompt(agent.id);
  }
}
