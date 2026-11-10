/**
 * Whether an agent message's reply buttons (markdown.ts, ``#reply=`` links) are answered: a turn of the user's
 * follows the message in the transcript, or a message of theirs is on its way to the agent (a "Sending…" bubble, a
 * queued message, a send held through a switch or a create), which always lands after everything already there.
 */

import type { MarkdownReplies } from "../markdown";
import { getChatById, getProvisionalChat, getQueuedMessagesForChat } from "../models/Chats";
import { getOutgoingMessages } from "../models/OutgoingMessages";
import type { AssistantMessageEvent, TranscriptEvent } from "../models/Response";
import { SEED_SOURCE, getEventsForChat, getRenderVersion, hasMoreAfter } from "../models/Response";
import { REPLY_HREF_PREFIX, canSendReply } from "./reply-senders";
import { isGenuineUserTurn } from "./turn-grouping";

interface ReplyIndex {
  /** The store's events and version it was read from: a chat's store bumps its version on every change, and one
   *  made anew starts its count again. */
  readonly events: readonly TranscriptEvent[];
  readonly renderVersion: number;
  /** The ids of the chat's loaded agent messages that offer replies and are followed by a user turn. */
  readonly answered: ReadonlySet<string>;
  /** The ids of all the chat's loaded agent messages that offer replies. */
  readonly offering: ReadonlySet<string>;
}

const indexByChat = new Map<string, ReplyIndex>();

/** Whether ``text`` may hold a reply link, a cheap check that spares every other message the transcript walk. */
export function offersReplies(text: string): boolean {
  return text.includes(REPLY_HREF_PREFIX);
}

function replyIndexOf(chatId: string): ReplyIndex {
  const renderVersion = getRenderVersion(chatId);
  const events = getEventsForChat(chatId);
  const cached = indexByChat.get(chatId);
  if (cached !== undefined && cached.renderVersion === renderVersion && cached.events === events) return cached;
  const offering = new Set<string>();
  const answered = new Set<string>();
  // Walked newest first, so a message is answered exactly when a user turn was met before it. A seed segment's
  // turns were written before anyone could answer, so they answer nothing.
  let isUserTurnAfter = false;
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (isGenuineUserTurn(event) && event.source !== SEED_SOURCE) {
      isUserTurnAfter = true;
    } else if (event.type === "assistant_message" && offersReplies(event.text ?? "")) {
      offering.add(event.event_id);
      if (isUserTurnAfter) answered.add(event.event_id);
    }
  }
  const index = { events, renderVersion, answered, offering };
  indexByChat.set(chatId, index);
  return index;
}

/** Whether a message of the user's is on its way to the chat's agent and not yet on its transcript. */
function hasPendingUserMessage(chatId: string): boolean {
  return (
    getOutgoingMessages(chatId).length > 0 ||
    getQueuedMessagesForChat(chatId).length > 0 ||
    (getChatById(chatId)?.handoff?.held_sends.length ?? 0) > 0 ||
    (getProvisionalChat(chatId)?.held_sends.length ?? 0) > 0
  );
}

/**
 * How ``event``'s reply buttons send, or undefined when it offers none. A message the chat's loaded transcript does
 * not hold (a reply still streaming as a draft, a sub-agent's) or one past the window's end, whose followers are not
 * loaded, reads as answered: nothing on the page can say it is still open.
 */
export function repliesFor(event: AssistantMessageEvent, chatId: string): MarkdownReplies | undefined {
  if (!offersReplies(event.text ?? "")) return undefined;
  const index = replyIndexOf(chatId);
  const isAnswered =
    !index.offering.has(event.event_id) ||
    index.answered.has(event.event_id) ||
    hasMoreAfter(chatId) ||
    hasPendingUserMessage(chatId);
  return { chatId, isAnswered };
}

/** What ``event``'s reply buttons are drawn from (``repliesFor``, and whether the chat has a composer to send
 *  through), for a memoized renderer to compare between renders. */
export function replyStateSignature(event: AssistantMessageEvent, chatId: string): string {
  const replies = repliesFor(event, chatId);
  return replies === undefined ? "" : `${replies.isAnswered}|${canSendReply(chatId)}`;
}
