/**
 * Which reply a chat waits on its user to answer: what the next-step strip is drawn under
 * (``views/NextStepSuggestions.ts``) and what a picker in a message is live in (``views/StudioPickers.ts``). Also
 * whether a reply is being worked on and whether anybody has spoken yet, which the activity and new-chat strips
 * follow (``views/ActionSuggestions.ts``).
 */

import { getChatById, getProvisionalChat, isLaunchInFlight } from "../models/Chats";
import { getOutgoingMessages } from "../models/OutgoingMessages";
import { getEventsForChat, hasMoreAfter, hasMoreBefore, isTranscriptLoaded } from "../models/Response";
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

/** Whether a chat the page has heard of only as one awaiting its first send is that still. */
function isAwaitingFirstSend(chatId: string): boolean {
  const provisional = getProvisionalChat(chatId);
  return provisional !== undefined && provisional.phase === "awaiting_first_send" && getChatById(chatId) === undefined;
}

/** Whether anybody has said anything in the chat, a message of this page's still on its way included; null while
 *  the page cannot tell, its transcript not having loaded. */
export function hasUserMessage(chatId: string): boolean | null {
  if (getOutgoingMessages(chatId).length > 0 || isLaunchInFlight(chatId)) return true;
  if ((getProvisionalChat(chatId)?.held_sends.length ?? 0) > 0) return true;
  if (getEventsForChat(chatId).some((event) => event.type === "user_message" && !isNonBoundaryUserMessage(event))) {
    return true;
  }
  // A window that stops short of the transcript's start is of a chat long enough to have been spoken in.
  if (hasMoreBefore(chatId)) return true;
  if (isTranscriptLoaded(chatId) || isAwaitingFirstSend(chatId)) return false;
  return null;
}

/** Whether no reply is being worked on in the chat: its turn is done as the next-step strip reads it, or nobody
 *  has said anything in it yet (a chat being created has an agent starting, and no reply to wait for). Null while
 *  the page knows neither the chat nor whether it has a message. */
export function isTurnDoneForActivity(chatId: string): boolean | null {
  if (getChatById(chatId) === undefined && getProvisionalChat(chatId) === undefined) return null;
  const hasMessage = hasUserMessage(chatId);
  if (hasMessage === null) return null;
  return hasMessage ? isTurnDoneForSuggestions(chatId) : true;
}
