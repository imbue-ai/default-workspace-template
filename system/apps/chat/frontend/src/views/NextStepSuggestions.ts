/**
 * The next-step strip: the suggested replies to the reply the chat waits on its user to answer
 * (``models/Suggestions.ts`` decides which set that is, from what is read of the chat here on every draw).
 * ``views/ChatSuggestions.ts`` draws it, with the other placements' strips. Once the reply is answered its set is
 * drawn in the transcript instead (``views/AnsweredReplySuggestions.ts``), from the same read of the chat.
 *
 * The chat app says when the chat's stored set changes, and the sets are read again. A click on a suggestion sends
 * it through the composer.
 */

import { isSuggestionSwitchOn } from "../models/ChatSettings";
import { addSuggestionsUpdatedListener, isRoomChat, removeSuggestionsUpdatedListener } from "../models/Chats";
import {
  collapseNextStepSuggestions,
  getNextStepSuggestions,
  reconcileNextStepSuggestions,
  rereadNextStepSuggestions,
  SUGGESTION_SWITCHES,
  toggleNextStepSuggestions,
} from "../models/Suggestions";
import type { NextStepFacts } from "../models/Suggestions";
import { isTurnDoneForSuggestions, lastReplyEventId } from "./last-reply";
import { submitSuggestion } from "./MessageInput";
import type { PlacementStrip } from "./suggestion-placement";

/**
 * Follow what changes the next-step sets of the chat on screen between draws: the chat app saying its stored set
 * changed has them read again (a set worked out since the turn ended arrives this way, seconds after the read the
 * turn's end asked for). Answers what stops following.
 */
export function followNextStepSuggestions(shownChatId: () => string | null): () => void {
  const rereadOnPush = (chatId: string): void => {
    if (chatId === shownChatId()) rereadNextStepSuggestions(chatId);
  };
  addSuggestionsUpdatedListener(rereadOnPush);
  return () => removeSuggestionsUpdatedListener(rereadOnPush);
}

/** Whether next-step suggestions are switched off, as far as the page knows: not while the settings still load. */
export function isNextStepSwitchedOff(): boolean {
  return isSuggestionSwitchOn(SUGGESTION_SWITCHES.next_step) === false;
}

/** What the page knows of the chat now, for deciding which of its sets is drawn where. */
export function nextStepFacts(chatId: string): NextStepFacts {
  return {
    isEnabled: isSuggestionSwitchOn(SUGGESTION_SWITCHES.next_step) === true,
    isRoom: isRoomChat(chatId),
    isTurnDone: isTurnDoneForSuggestions(chatId),
    lastReplyEventId: lastReplyEventId(chatId),
  };
}

/** The chat's next-step strip as it stands now, or null when no set is ready for the reply it waits on. */
export function nextStepStrip(chatId: string): PlacementStrip | null {
  reconcileNextStepSuggestions(chatId, nextStepFacts(chatId));
  const adopted = getNextStepSuggestions(chatId);
  if (adopted === null) return null;
  return {
    placement: "next_step",
    // The reply the set follows, so the next reply's pills rise in rather than changing in place.
    setKey: adopted.set.after_event_id,
    suggestions: adopted.set.suggestions,
    isOpen: adopted.isOpen,
    onPick: (suggestion) => submitSuggestion(chatId, suggestion.label),
    onToggle: () => toggleNextStepSuggestions(chatId),
    collapse: () => collapseNextStepSuggestions(chatId),
  };
}
