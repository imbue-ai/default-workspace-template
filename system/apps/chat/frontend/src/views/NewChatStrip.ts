/**
 * The new-chat strip ("Things to try"): things worth trying in Studio, on a chat nobody has spoken in. Each is a
 * suggestion whose click points the user at a control rather than sending anything
 * (``models/NewChatSuggestions.ts`` decides what the strip holds, from what is read of the chat here on every draw;
 * ``views/suggestion-actions.ts`` does a click). ``views/ChatSuggestions.ts`` draws it.
 */

import { isRoomChat } from "../models/Chats";
import { getNewChatSuggestions, reconcileNewChatSuggestions } from "../models/NewChatSuggestions";
import { hasUserMessage } from "./last-reply";
import { canRunSuggestionAction, runSuggestionAction } from "./suggestion-actions";
import type { PlacementStrip } from "./suggestion-placement";

/** The chat's new-chat strip as it stands now, or null when the chat has none. */
export function newChatStrip(chatId: string, isEnabled: boolean): PlacementStrip | null {
  reconcileNewChatSuggestions(chatId, {
    isRoom: isRoomChat(chatId),
    hasUserMessage: hasUserMessage(chatId),
    isEnabled,
    canActOn: (suggestion) => canRunSuggestionAction(suggestion.action),
  });
  const suggestions = getNewChatSuggestions(chatId);
  if (suggestions === null) return null;
  return {
    placement: "new_chat",
    setKey: "new_chat",
    suggestions,
    isOpen: true,
    onPick: (picked) => {
      const suggestion = suggestions.find((entry) => entry.suggestion_id === picked.suggestion_id);
      if (suggestion !== undefined) runSuggestionAction(suggestion.action);
    },
    onToggle: null,
  };
}
