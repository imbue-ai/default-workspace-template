/**
 * New-chat suggestions: things worth trying in Studio that the workspace's owner has not done yet, offered on a
 * chat nobody has said anything in. Each is an action suggestion: a click does something other than send its label
 * (``views/suggestion-actions.ts``).
 *
 * The chat app decides which are offered (``GET /api/suggestions/new-chat``, the backend's
 * ``new_chat_suggestions.py``); it answers nothing to anyone but the workspace's owner, before the tour has
 * finished, or with new-chat suggestions switched off. This module reads the answer per chat, and keeps, per chat,
 * the strip the page draws: the first few suggestions the page can act on.
 *
 * The strip is there on a chat that had no user message when the page first looked, and goes for good on the
 * chat's first message.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { addChatSettingsUpdatedListener } from "./ChatSettings";
import type { ActionSuggestion, SuggestionAction } from "./Suggestions";

/** How many suggestions the strip shows, of those the page can act on. */
export const MAX_NEW_CHAT_SUGGESTIONS_SHOWN = 3;
export const RETRY_DELAY_MS = 30_000;

// Every action this page knows, so a suggestion carrying any other is dropped. Keyed by the type, so an action
// added to it cannot be left out here.
const ACTION_BY_NAME: Record<SuggestionAction, SuggestionAction> = {
  new_chat_coach: "new_chat_coach",
  phone_access: "phone_access",
  build_app_coach: "build_app_coach",
  connect_data_coach: "connect_data_coach",
};
const KNOWN_ACTIONS: ReadonlyMap<string, SuggestionAction> = new Map(Object.entries(ACTION_BY_NAME));

/** What ``GET /api/suggestions/new-chat`` answers: every suggestion that is offered, in a fixed order. */
export interface NewChatAnswer {
  suggestions: readonly { suggestion_id: string; label: string; action?: string | null }[];
}

/** The suggestions of an answer this page can draw: those whose action it knows. One with no action, or with an
 *  action a newer backend added, is dropped, so no pill is ever dead. */
export function actionSuggestionsOf(answer: NewChatAnswer): ActionSuggestion[] {
  const suggestions: ActionSuggestion[] = [];
  for (const suggestion of answer.suggestions) {
    const action = typeof suggestion.action === "string" ? KNOWN_ACTIONS.get(suggestion.action) : undefined;
    if (action === undefined) continue;
    suggestions.push({
      suggestion_id: suggestion.suggestion_id,
      label: suggestion.label,
      action,
      is_recommended: false,
    });
  }
  return suggestions;
}

/** What the page knows of the chat when it decides whether to draw the strip. */
export interface NewChatFacts {
  isRoom: boolean;
  // Whether anybody has said anything in the chat; null while the page cannot tell.
  hasUserMessage: boolean | null;
  // Whether new-chat suggestions are switched on (known to be: false while the settings load).
  isEnabled: boolean;
  // Whether this page can do what a click on the suggestion asks for.
  canActOn: (suggestion: ActionSuggestion) => boolean;
}

// The strip of a chat: not decided before the page knows whether the chat has a message, then there on a chat that
// had none when the page first looked, and gone on any other, and from the chat's first message on.
type NewChatPhase = "undecided" | "open" | "gone";

interface ChatNewChatState {
  phase: NewChatPhase;
  // What the last answer carried; null before one.
  suggestions: readonly ActionSuggestion[] | null;
  // Counts the reads asked for, so an answer that a later read has overtaken is dropped.
  latestRead: number;
  isReading: boolean;
  // A read is wanted and none has answered yet.
  isReadOwed: boolean;
  // After a failed read, when the page's looks may ask again: they come on every render.
  retryNotBefore: number;
  drawn: readonly ActionSuggestion[] | null;
}

const stateByChat = new Map<string, ChatNewChatState>();

function stateOf(chatId: string): ChatNewChatState {
  let state = stateByChat.get(chatId);
  if (state === undefined) {
    state = {
      phase: "undecided",
      suggestions: null,
      latestRead: 0,
      isReading: false,
      isReadOwed: false,
      retryNotBefore: 0,
      drawn: null,
    };
    stateByChat.set(chatId, state);
  }
  return state;
}

/** Ask for a read whatever stands in its way: one already out is overtaken, and a failed one's wait is over. */
function oweRead(state: ChatNewChatState): void {
  state.latestRead++;
  state.isReading = false;
  state.isReadOwed = true;
  state.retryNotBefore = 0;
}

let isFollowingChatSettings = false;

/** A switch changed somewhere: what the route answers depends on it, so every strip still there is read again.
 *  Registered on the first look rather than as this module loads. */
function followChatSettings(): void {
  if (isFollowingChatSettings) return;
  isFollowingChatSettings = true;
  addChatSettingsUpdatedListener(() => {
    for (const state of stateByChat.values()) {
      if (state.phase === "open") oweRead(state);
    }
  });
}

/** Read what the chat app offers ``chatId``. A read that fails is warned about and keeps what was read before;
 *  one the page still owes is asked for again by a later look, after ``RETRY_DELAY_MS``. */
export async function readNewChatSuggestions(chatId: string): Promise<void> {
  const state = stateOf(chatId);
  const attempt = ++state.latestRead;
  state.isReading = true;
  try {
    const answer = await m.request<NewChatAnswer>({
      method: "GET",
      url: apiUrl("/api/suggestions/new-chat"),
      params: { chat_id: chatId },
    });
    if (attempt !== state.latestRead) return;
    state.suggestions = actionSuggestionsOf(answer);
    state.isReadOwed = false;
  } catch (error) {
    if (attempt !== state.latestRead) return;
    console.warn(`Failed to read the new-chat suggestions of chat ${chatId}`, error);
    state.retryNotBefore = Date.now() + RETRY_DELAY_MS;
  } finally {
    if (attempt === state.latestRead) state.isReading = false;
  }
  m.redraw();
}

function followFirstMessage(state: ChatNewChatState, hasUserMessage: boolean | null): void {
  if (hasUserMessage === null) return;
  if (state.phase === "undecided") {
    state.phase = hasUserMessage ? "gone" : "open";
    if (!hasUserMessage) oweRead(state);
    return;
  }
  if (hasUserMessage) state.phase = "gone";
}

/**
 * Bring the chat's strip in line with what the page now knows, reading what the chat app offers when the page
 * first looks at a chat with no message.
 *
 * The strip is drawn only with something to show: the first ``MAX_NEW_CHAT_SUGGESTIONS_SHOWN`` of what was read
 * that the page can act on, while new-chat suggestions are switched on. A room has none.
 */
export function reconcileNewChatSuggestions(chatId: string, facts: NewChatFacts): void {
  followChatSettings();
  const state = stateOf(chatId);
  if (facts.isRoom) {
    state.drawn = null;
    return;
  }
  followFirstMessage(state, facts.hasUserMessage);
  if (state.phase !== "open" || !facts.isEnabled) {
    state.drawn = null;
    return;
  }
  if (state.isReadOwed && !state.isReading && Date.now() >= state.retryNotBefore) {
    void readNewChatSuggestions(chatId);
  }
  const shown = (state.suggestions ?? []).filter(facts.canActOn).slice(0, MAX_NEW_CHAT_SUGGESTIONS_SHOWN);
  state.drawn = shown.length === 0 ? null : shown;
}

/** The suggestions of the strip the chat's page draws, as of the last ``reconcileNewChatSuggestions``, or null
 *  when it draws none. */
export function getNewChatSuggestions(chatId: string): readonly ActionSuggestion[] | null {
  return stateByChat.get(chatId)?.drawn ?? null;
}

/** Forget every chat's new-chat suggestions (tests). */
export function resetNewChatSuggestionsForTests(): void {
  stateByChat.clear();
}
