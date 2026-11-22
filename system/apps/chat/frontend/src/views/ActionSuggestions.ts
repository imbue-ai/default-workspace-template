/**
 * The activity strip ("While you wait") and the new-chat strip ("Things to try"): things worth trying in Studio,
 * each a suggestion whose click points the user at a control rather than sending anything
 * (``models/ActivitySuggestions.ts`` decides what each strip holds and whether it is open, from what is read of the
 * chat here on every draw; ``views/suggestion-actions.ts`` does a click). ``views/ChatSuggestions.ts`` draws them,
 * with the next-step strip.
 */

import {
  collapseActionStrip,
  getActionStrip,
  reconcileActivitySuggestions,
  recordActionSuggestionClicked,
  toggleActionStrip,
} from "../models/ActivitySuggestions";
import type { ActionPlacement, ActionStrip } from "../models/ActivitySuggestions";
import { isRoomChat } from "../models/Chats";
import { hasUserMessage, isTurnDoneForActivity } from "./last-reply";
import { canRunSuggestionAction, runSuggestionAction } from "./suggestion-actions";
import type { PlacementStrip } from "./suggestion-placement";

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
  reconcileActivitySuggestions(chatId, {
    isRoom: isRoomChat(chatId),
    isTurnDone: isTurnDoneForActivity(chatId),
    hasUserMessage: hasUserMessage(chatId),
    isEnabled,
    canActOn: (suggestion) => canRunSuggestionAction(suggestion.action),
  });
  const strips: PlacementStrip[] = [];
  for (const placement of ["activity", "new_chat"] as const) {
    const strip = getActionStrip(chatId, placement);
    if (strip !== null) strips.push(placementStrip(chatId, placement, strip));
  }
  return strips;
}
