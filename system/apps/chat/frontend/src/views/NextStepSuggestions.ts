/**
 * The next-step strip: the suggested replies to the agent's last reply (``models/Suggestions.ts`` decides which set
 * is drawn, from what is read of the chat here on every draw). ``views/ChatSuggestions.ts`` draws it, with the
 * other placements' strips.
 *
 * The chat app says when the chat's stored set changes, and the set is read again. A click on a suggestion sends it
 * through the composer.
 */

import { addSuggestionsUpdatedListener, isRoomChat, removeSuggestionsUpdatedListener } from "../models/Chats";
import { addMessageSentListener, removeMessageSentListener } from "../models/Response";
import {
  collapseNextStepSuggestions,
  getNextStepSuggestions,
  reconcileNextStepSuggestions,
  rereadNextStepSuggestions,
  toggleNextStepSuggestions,
} from "../models/Suggestions";
import { isTurnDoneForSuggestions, lastReplyEventId } from "./last-reply";
import { submitSuggestion } from "./MessageInput";
import type { PlacementStrip } from "./suggestion-placement";

// How many of a generated set's suggestions are drawn before "Show more". A fixed set is drawn whole.
const GENERATED_SET_FOLD = 3;

/**
 * Follow what changes the next-step set of the chat on screen between draws: a message sent in it collapses its
 * open strip, and the chat app saying its stored set changed has it read again (a set worked out since the turn
 * ended arrives this way, seconds after the read the turn's end asked for). Answers what stops following.
 */
export function followNextStepSuggestions(shownChatId: () => string | null): () => void {
  const collapseOnSend = (chatId: string): void => {
    if (chatId === shownChatId()) collapseNextStepSuggestions(chatId);
  };
  const rereadOnPush = (chatId: string): void => {
    if (chatId === shownChatId()) rereadNextStepSuggestions(chatId);
  };
  addMessageSentListener(collapseOnSend);
  addSuggestionsUpdatedListener(rereadOnPush);
  return () => {
    removeMessageSentListener(collapseOnSend);
    removeSuggestionsUpdatedListener(rereadOnPush);
  };
}

/** The chat's next-step strip as it stands now, or null when it has none. */
export function nextStepStrip(chatId: string, isEnabled: boolean): PlacementStrip | null {
  reconcileNextStepSuggestions(chatId, {
    isEnabled,
    isRoom: isRoomChat(chatId),
    isTurnDone: isTurnDoneForSuggestions(chatId),
    lastReplyEventId: lastReplyEventId(chatId),
  });
  const adopted = getNextStepSuggestions(chatId);
  if (adopted === null) return null;
  return {
    placement: "next_step",
    // The reply the set follows, so the next reply's strip is another one.
    setKey: adopted.set.after_event_id,
    suggestions: adopted.set.suggestions,
    foldAfter: adopted.set.source === "fixed" ? null : GENERATED_SET_FOLD,
    isOpen: adopted.isOpen,
    onPick: (suggestion) => submitSuggestion(chatId, suggestion.label),
    onToggle: () => toggleNextStepSuggestions(chatId),
    collapse: () => collapseNextStepSuggestions(chatId),
  };
}
