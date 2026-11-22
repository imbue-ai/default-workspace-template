/**
 * The activity strip ("While you wait") and the new-chat strip ("Things to try"): things worth trying in Studio,
 * each a suggestion whose click points the user at a control rather than sending anything
 * (``models/ActivitySuggestions.ts`` decides what each strip holds and whether it is open, from what is read of the
 * chat here on every draw; ``views/suggestion-actions.ts`` does a click). ``views/ChatSuggestions.ts`` draws them,
 * with the next-step strip.
 *
 * The same read follows the chat's turns, so this is also where the page tells the shell whether a reply is under
 * way in the chat it shows (``reportAgentWorking``).
 */

import {
  ACTION_PLACEMENTS,
  collapseActionStrip,
  getActionStrip,
  isReplyUnderWay,
  reconcileActivitySuggestions,
  recordActionSuggestionClicked,
  toggleActionStrip,
} from "../models/ActivitySuggestions";
import type { ActionPlacement, ActionStrip } from "../models/ActivitySuggestions";
import { isRoomChat } from "../models/Chats";
import { addMessageSentListener, removeMessageSentListener } from "../models/Response";
import { reportAgentWorking } from "../shell";
import { hasUserMessage, isTurnDoneForActivity } from "./last-reply";
import { canRunSuggestionAction, runSuggestionAction } from "./suggestion-actions";
import type { PlacementStrip } from "./suggestion-placement";

/**
 * Follow what changes the action strips of the chat on screen between draws: a message sent in it collapses its
 * open new-chat strip, one opened again from its toggle included. The activity strip is left alone, since the turn
 * opens and closes it. Answers what stops following, which also takes back what the page told the shell of a reply
 * under way, since nothing reads the chat for it from then on.
 */
export function followActionSuggestions(shownChatId: () => string | null): () => void {
  const collapseOnSend = (chatId: string): void => {
    if (chatId === shownChatId()) collapseActionStrip(chatId, "new_chat");
  };
  addMessageSentListener(collapseOnSend);
  return () => {
    removeMessageSentListener(collapseOnSend);
    reportAgentWorking(false);
  };
}

function placementStrip(chatId: string, placement: ActionPlacement, strip: ActionStrip): PlacementStrip {
  return {
    placement,
    setKey: placement,
    suggestions: strip.suggestions,
    foldAfter: null,
    isOpen: strip.isOpen,
    onPick: (picked) => {
      const suggestion = strip.suggestions.find((entry) => entry.suggestion_id === picked.suggestion_id);
      if (suggestion === undefined) return;
      runSuggestionAction(suggestion.action);
      void recordActionSuggestionClicked(chatId, suggestion.suggestion_id);
    },
    onToggle: () => toggleActionStrip(chatId, placement),
    collapse: () => collapseActionStrip(chatId, placement),
  };
}

/** The chat's activity and new-chat strips as they stand now: either, both, or neither. */
export function actionStrips(chatId: string, isEnabled: Record<ActionPlacement, boolean>): PlacementStrip[] {
  const isRoom = isRoomChat(chatId);
  reconcileActivitySuggestions(chatId, {
    isRoom,
    isTurnDone: isTurnDoneForActivity(chatId),
    hasUserMessage: hasUserMessage(chatId),
    isEnabled,
    canActOn: (suggestion) => canRunSuggestionAction(suggestion.action),
  });
  // A room's reader works on no reply.
  reportAgentWorking(!isRoom && isReplyUnderWay(chatId));
  const strips: PlacementStrip[] = [];
  for (const placement of ACTION_PLACEMENTS) {
    const strip = getActionStrip(chatId, placement);
    if (strip !== null) strips.push(placementStrip(chatId, placement, strip));
  }
  return strips;
}
