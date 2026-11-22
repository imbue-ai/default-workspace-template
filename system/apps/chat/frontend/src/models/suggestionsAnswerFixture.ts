/** What the chat app answers of a chat's next-step suggestions, for tests (``GET /api/chats/<chat-id>/suggestions``). */

import type { AnsweredSuggestionSet, SuggestionsAnswer } from "./Suggestions";

/** The answer for a chat with no stored set and nothing remembered. */
export const NO_SUGGESTIONS_ANSWER: SuggestionsAnswer = {
  status: "none",
  after_event_id: null,
  source: null,
  suggestions: [],
  sets: [],
};

/** The answer while a set for the reply ``afterEventId`` is being worked out, with nothing remembered. */
export function pendingSuggestionsAnswerFixture(afterEventId: string): SuggestionsAnswer {
  return { status: "pending", after_event_id: afterEventId, source: null, suggestions: [], sets: [] };
}

/** A set for the reply ``afterEventId``: one suggestion per label, none recommended. */
export function suggestionSetFixture(
  afterEventId: string,
  labels: readonly string[],
  source: string,
): AnsweredSuggestionSet {
  return {
    after_event_id: afterEventId,
    source,
    suggestions: labels.map((label) => ({ suggestion_id: label.toLowerCase(), label, is_recommended: false })),
  };
}

/** The answer with ``sets`` remembered, oldest first, the last of them the one stored. */
export function storedSuggestionsAnswerFixture(sets: readonly AnsweredSuggestionSet[]): SuggestionsAnswer {
  return { status: "ready", ...sets[sets.length - 1], sets };
}
