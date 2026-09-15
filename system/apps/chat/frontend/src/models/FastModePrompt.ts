/**
 * State for the fast-mode grace-period prompt.
 *
 * The workspace's first chat launches with fast mode on (the `first` create
 * template) so the opening conversation feels responsive. Fast mode costs more
 * per token, so once that chat has run its grace period (see fast-mode-prompt.ts)
 * the user is asked whether to keep it -- once per agent, ever. Any exit from the
 * modal records the answer; only "switch to standard speed" also changes the
 * agent's live setting. The answer never affects later creates: no other chat
 * launches fast in the first place.
 *
 * The durable record is the agent label `fast_mode_prompt_answered=true`
 * (written via the backend latch endpoint). Labels reach the frontend with the
 * next observe relist, which can lag by minutes on an idle workspace, so an
 * in-session set of answered agent ids suppresses the prompt in the meantime.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { setFastMode } from "./ModelSettings";

export const FAST_MODE_ANSWERED_LABEL = "fast_mode_prompt_answered";

// The chat whose conversation raised the prompt, or null when none is showing.
// Also the chat the answer is applied to live, since it is the one being used.
let promptingChatId: string | null = null;
// Chats answered this session -- the immediate suppressor while the label write
// propagates through the observe relist.
const answeredChatIds = new Set<string>();

export function getFastModePromptChatId(): string | null {
  return promptingChatId;
}

/** Whether this agent's prompt has been answered -- by the durable label or in
 *  this session while the label write is still propagating. */
export function isFastModePromptAnswered(chatId: string, labels: Record<string, string> | undefined): boolean {
  return answeredChatIds.has(chatId) || labels?.[FAST_MODE_ANSWERED_LABEL] === "true";
}

/** Raise the prompt on behalf of `chatId`. The first conversation to claim it
 *  keeps it until it is answered: every mounted ChatPanel re-runs its check on
 *  every render, so letting a second agent take over would flip the owner (and
 *  schedule a redraw) on every frame while both are waiting. */
export function openFastModePrompt(chatId: string): void {
  if (promptingChatId !== null) {
    return;
  }
  promptingChatId = chatId;
  m.redraw();
}

/**
 * Record the user's answer and apply it to the agent that raised the prompt.
 *
 * Every exit from the modal routes here -- the prompt asks once per agent, so
 * any interaction latches it as answered. Dismissing (backdrop, Escape) routes
 * with `false`, since turning fast mode off is the outcome that cannot surprise
 * anyone with a bill.
 */
export function resolveFastModePrompt(isFastModeEnabled: boolean): void {
  const chatId = promptingChatId;
  promptingChatId = null;
  if (chatId === null) {
    return;
  }
  // Latch in-session immediately so the prompt cannot re-fire while the label
  // write is in flight (or if it fails -- re-asking after a reload beats
  // re-asking on the next render).
  answeredChatIds.add(chatId);
  m.redraw();

  if (!isFastModeEnabled) {
    // Only a switch to standard speed needs sending: the agent is already fast.
    // Dispatches through the agent's own harness resolver on the backend.
    setFastMode(chatId, false);
  }

  void m
    .request({
      method: "POST",
      url: apiUrl(`/api/chats/${encodeURIComponent(chatId)}/fast-mode-answered`),
    })
    .catch((error) => {
      // The live agent still got the change; only the durable latch is lost, so
      // the prompt could reappear after a reload rather than silently sticking.
      console.warn("Failed to record the fast-mode answer", error);
    });
}
