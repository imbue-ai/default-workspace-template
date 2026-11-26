/**
 * The suggestions a reply keeps once it has been answered: in the transcript under the reply, the quiet line the
 * next-step strip is above the composer (``views/SuggestionStrip.ts``), which shows the set's pills under itself
 * and hides them again. ``models/Suggestions.ts`` holds every set the page knows and whether each is shown, since a
 * row of the transcript is unmounted as it scrolls out of the window.
 *
 * A click on a pill sends its label through the composer now, as a pill above the composer does. The line offers no
 * way to switch the placement off: that is the strip's, above the composer.
 */

import m from "mithril";
import { getAnsweredReplySuggestions, toggleAnsweredReplySuggestions } from "../models/Suggestions";
import { submitSuggestion } from "./MessageInput";
import { nextStepFacts } from "./NextStepSuggestions";
import { suggestionLine, suggestionPills } from "./SuggestionStrip";

/**
 * The line of the last answered reply among ``eventIds`` that the page knows a set for, or null when it knows none,
 * or draws none in the transcript now. ``eventIds`` are the assistant messages one part of the transcript draws, in
 * order.
 */
export function answeredReplySuggestions(chatId: string, eventIds: readonly string[]): m.Vnode | null {
  for (const eventId of [...eventIds].reverse()) {
    const drawn = getAnsweredReplySuggestions(chatId, eventId, () => nextStepFacts(chatId));
    if (drawn === null) continue;
    return m(
      "div",
      {
        class: "answered-reply-suggestions flex flex-col gap-1.5",
        "data-after-event-id": eventId,
        role: "group",
        "aria-label": "Suggestions",
      },
      [
        suggestionLine({
          count: drawn.set.suggestions.length,
          isOpen: drawn.isOpen,
          onToggle: () => toggleAnsweredReplySuggestions(chatId, eventId),
          onDisable: null,
        }),
        drawn.isOpen
          ? suggestionPills(drawn.set.suggestions, (suggestion) => submitSuggestion(chatId, suggestion.label))
          : null,
      ],
    );
  }
  return null;
}
