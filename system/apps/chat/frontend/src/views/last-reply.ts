/**
 * Which reply a chat waits on its user to answer: what the next-step strip is drawn under
 * (``views/NextStepSuggestions.ts``) and what a picker in a message is live in (``views/StudioPickers.ts``).
 */

import { getChatById, getProvisionalChat, isLaunchInFlight } from "../models/Chats";
import { getOutgoingMessages } from "../models/OutgoingMessages";
import { getEventsForChat, hasMoreAfter } from "../models/Response";
import { isNonBoundaryUserMessage } from "./message-classification";
import { isChatTurnOver } from "./TurnDoneMark";

/** Whether the chat's turn is done, as the done mark reads it. A chat still awaiting its first send (a seeded one,
 *  before its first agent) has no turn to be in, and counts as done until something is sent. */
export function isTurnDoneForSuggestions(chatId: string): boolean {
  if (isChatTurnOver(chatId)) return true;
  const provisional = getProvisionalChat(chatId);
  return (
    provisional !== undefined &&
    provisional.phase === "awaiting_first_send" &&
    getChatById(chatId) === undefined &&
    !isLaunchInFlight(chatId) &&
    getOutgoingMessages(chatId).length === 0
  );
}

/** The event id of the reply a set of suggestions may follow: the last assistant message the page holds, when the
 *  page holds the transcript's end and nobody has said anything since. */
export function lastReplyEventId(chatId: string): string | null {
  if (hasMoreAfter(chatId)) return null;
  const events = getEventsForChat(chatId);
  for (let index = events.length - 1; index >= 0; index--) {
    const event = events[index];
    if (event.type === "assistant_message") return event.event_id;
    if (event.type === "user_message" && !isNonBoundaryUserMessage(event)) return null;
  }
  return null;
}

/** Whether the assistant message ``eventId`` is the reply the chat waits on its user to answer: its last one, with
 *  nothing said since and its turn done. */
export function isReplyAwaitingAnswer(chatId: string, eventId: string): boolean {
  return lastReplyEventId(chatId) === eventId && isTurnDoneForSuggestions(chatId);
}
