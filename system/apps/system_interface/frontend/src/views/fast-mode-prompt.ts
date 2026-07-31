/**
 * When a conversation has used up its fast-mode grace period.
 *
 * A new chat runs fast so it feels responsive, then asks whether to keep paying
 * for that (see WorkspaceFastMode.ts). This module decides when "then" is: it
 * counts the conversation's completed user turns and reports whether the prompt
 * is now owed.
 *
 * A turn is counted exactly as the transcript view counts one, by reusing the
 * same boundary rule the timeline groups on -- so "5 turns" means the five
 * exchanges the user can actually see, not five raw transcript lines. Permission
 * verdicts are excluded on top of that: the timeline treats them as turn
 * boundaries (see buildSections) but they are the app talking to itself, not the
 * user taking another turn.
 */

import type { TranscriptEvent } from "../models/Response";
import { getModelSettings } from "../models/ModelSettings";
import { getWorkspaceFastMode, openFastModePrompt } from "../models/WorkspaceFastMode";
import { isNonBoundaryUserMessage, parsePermissionResolution } from "./message-classification";

/** How many user turns a chat runs with fast mode on before it asks whether to
 *  keep it. The one knob for the grace period. */
export const FAST_MODE_GRACE_TURN_COUNT = 5;

/** How many turns the user has actually taken in this conversation. */
export function countUserTurns(events: TranscriptEvent[]): number {
  let count = 0;
  for (const event of events) {
    if (event.type !== "user_message") {
      continue;
    }
    const content = event.content ?? "";
    if (isNonBoundaryUserMessage(content, event.is_meta)) {
      continue;
    }
    if (parsePermissionResolution(content) !== null) {
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
 * prompt would ask.
 */
export function isFastModePromptOwed(agentId: string, events: TranscriptEvent[], isAgentIdle: boolean): boolean {
  const workspaceFastMode = getWorkspaceFastMode();
  if (workspaceFastMode === null || workspaceFastMode.fast_mode !== null) {
    return false;
  }
  if (!isAgentIdle) {
    return false;
  }
  const settings = getModelSettings(agentId);
  if (settings === null || !settings.fast_mode) {
    return false;
  }
  return countUserTurns(events) >= FAST_MODE_GRACE_TURN_COUNT;
}

/** Raise the prompt if this conversation has earned it. Safe to call on every
 *  render: opening is idempotent, and the gates that walk the transcript sit
 *  behind the cheap ones (see isFastModePromptOwed). */
export function maybePromptForFastMode(agentId: string, events: TranscriptEvent[], isAgentIdle: boolean): void {
  if (isFastModePromptOwed(agentId, events, isAgentIdle)) {
    openFastModePrompt(agentId);
  }
}
