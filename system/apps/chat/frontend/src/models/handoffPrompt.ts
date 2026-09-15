/**
 * The prompt a handoff's successor is started with, as the wire carries it: a chip whose opening
 * words are the backend's ``HANDOFF_PROMPT_PREFIX``. The transcript walk folds it into the
 * handoff node, and the optimistic-send layer must not take it for a sent message's arrival.
 */

import type { UserMessageEvent } from "./Response";

const HANDOFF_PROMPT_PREFIX = 'You are continuing the chat "';

export function isHandoffPromptChip(event: Pick<UserMessageEvent, "content" | "display">): boolean {
  return event.display === "chip" && event.content.trimStart().startsWith(HANDOFF_PROMPT_PREFIX);
}
