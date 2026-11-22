/**
 * Suggestions: short entries above the composer that the user can click instead of typing (the backend's
 * ``suggestions.py``).
 *
 * A chat's next-step suggestions are one stored set, offered under one assistant message
 * (``GET /api/chats/<chat-id>/suggestions``). This module reads the set and keeps, per chat, the one the page has
 * adopted: the set it draws, open or collapsed to its toggle. A set is adopted only for the reply it names, once that
 * reply's turn is done, so a set left over from an earlier reply is never drawn: after a reload, after a reply that
 * arrived while the page was away, or on a second device.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { addChatSettingsUpdatedListener, type SuggestionSwitch } from "./ChatSettings";

/** Where a strip of suggestions is offered: under a reply, while waiting for one, or on a chat with no message yet
 *  (the backend's ``SuggestionPlacement``). */
export type SuggestionPlacement = "next_step" | "activity" | "new_chat";

/** The workspace setting that switches each placement on and off. */
export const SUGGESTION_SWITCHES: Record<SuggestionPlacement, SuggestionSwitch> = {
  next_step: "is_next_step_suggestions_enabled",
  activity: "is_activity_suggestions_enabled",
  new_chat: "is_new_chat_suggestions_enabled",
};

/** One clickable entry (the backend's ``Suggestion``, without its action). Clicking a reply suggestion sends its
 *  label as the user's message. */
export interface Suggestion {
  // Unique within its strip.
  suggestion_id: string;
  label: string;
  is_recommended: boolean;
}

/** What clicking an action suggestion does (the backend's ``SuggestionAction``). */
export type SuggestionAction = "new_chat_coach" | "phone_access" | "build_app_coach" | "connect_data_coach";

/** A suggestion whose click does something other than send its label (``models/ActivitySuggestions.ts``). A
 *  next-step suggestion is never one. */
export interface ActionSuggestion extends Suggestion {
  action: SuggestionAction;
}

/** Where a set came from: the chat app itself, with no model asked, or worked out by a model. */
export type SuggestionSource = "fixed" | "generated";

/** The suggestions offered under one assistant message. */
export interface SuggestionSet {
  // The event id of the assistant message the set follows.
  after_event_id: string;
  source: SuggestionSource | null;
  suggestions: readonly Suggestion[];
}

/** What ``GET /api/chats/<chat-id>/suggestions`` answers: ``ready`` with the stored set, ``pending`` while one is being
 *  worked out (a ``suggestions_updated`` push follows when one is stored), or ``none``. An ``action`` marks a
 *  suggestion whose click does something other than send its label. */
export interface SuggestionsAnswer {
  status: string;
  after_event_id: string | null;
  source: string | null;
  suggestions: readonly (Suggestion & { action?: string | null })[];
}

/**
 * The set an answer carries, or null when it carries none to draw: any status but "ready" (one this page does not
 * know included), or a set with nothing left in it. A suggestion with an action is dropped, since a next-step set
 * carries none, so a newer backend never produces a dead pill; and only the first suggestion may be the recommended
 * one.
 */
export function suggestionSetOf(answer: SuggestionsAnswer): SuggestionSet | null {
  if (answer.status !== "ready" || answer.after_event_id === null) return null;
  const suggestions = answer.suggestions
    .filter((suggestion) => suggestion.action === undefined || suggestion.action === null)
    .map((suggestion, index) => ({
      suggestion_id: suggestion.suggestion_id,
      label: suggestion.label,
      is_recommended: suggestion.is_recommended && index === 0,
    }));
  if (suggestions.length === 0) return null;
  const source = answer.source === "fixed" || answer.source === "generated" ? answer.source : null;
  return { after_event_id: answer.after_event_id, source, suggestions };
}

/** The set a chat's page draws. */
export interface AdoptedSuggestions {
  set: SuggestionSet;
  // Drawn as its pills, rather than collapsed to its toggle.
  isOpen: boolean;
}

/** What the page knows of the chat when it decides which set to draw. */
export interface NextStepFacts {
  // Next-step suggestions are switched on (known to be: false while the settings load).
  isEnabled: boolean;
  isRoom: boolean;
  // The chat's turn is over, or it has none to be in yet.
  isTurnDone: boolean;
  // The event id of the reply a set may follow: the last assistant message the page holds, with nothing said after it.
  lastReplyEventId: string | null;
}

interface ChatSuggestionState {
  // The set the last answer carried.
  fetched: SuggestionSet | null;
  adopted: (AdoptedSuggestions & { hasTurnStartedSince: boolean }) | null;
  // Whether the turn was done at the last look; null before the first.
  wasTurnDone: boolean | null;
  // Counts the reads asked for, so an answer that a later read has overtaken is dropped.
  latestRead: number;
  isReading: boolean;
  // A read is wanted and none has answered yet: from the page's first look, and again from each turn's end.
  isReadOwed: boolean;
  // After a failed read, when the page's looks may ask again: they come on every render.
  retryNotBefore: number;
}

export const RETRY_DELAY_MS = 30_000;

const stateByChat = new Map<string, ChatSuggestionState>();

function stateOf(chatId: string): ChatSuggestionState {
  let state = stateByChat.get(chatId);
  if (state === undefined) {
    state = {
      fetched: null,
      adopted: null,
      wasTurnDone: null,
      latestRead: 0,
      isReading: false,
      isReadOwed: true,
      retryNotBefore: 0,
    };
    stateByChat.set(chatId, state);
  }
  return state;
}

function isSameSet(left: SuggestionSet | undefined, right: SuggestionSet): boolean {
  return left !== undefined && JSON.stringify(left) === JSON.stringify(right);
}

/** Read the chat's stored set. A read that fails is warned about and keeps the set read before; one the page still
 *  owes is asked for again by a later look, after ``RETRY_DELAY_MS``. */
export async function readNextStepSuggestions(chatId: string): Promise<void> {
  const state = stateOf(chatId);
  const read = ++state.latestRead;
  state.isReading = true;
  try {
    const answer = await m.request<SuggestionsAnswer>({
      method: "GET",
      url: apiUrl("/api/chats/:chatId/suggestions"),
      params: { chatId },
    });
    if (read !== state.latestRead) return;
    state.fetched = suggestionSetOf(answer);
    state.isReadOwed = false;
  } catch (error) {
    if (read !== state.latestRead) return;
    console.warn(`Failed to read the suggestions of chat ${chatId}`, error);
    state.retryNotBefore = Date.now() + RETRY_DELAY_MS;
  } finally {
    if (read === state.latestRead) state.isReading = false;
  }
  m.redraw();
}

/** Read the set again for a chat this page draws suggestions for: the page is shown again, or the chat app said its
 *  stored set changed (a set worked out since the turn ended arrives this way, seconds after the read the turn's end
 *  asked for). */
export function rereadNextStepSuggestions(chatId: string): void {
  if (stateByChat.has(chatId)) void readNextStepSuggestions(chatId);
}

addChatSettingsUpdatedListener(() => {
  for (const chatId of stateByChat.keys()) void readNextStepSuggestions(chatId);
});

/**
 * Bring the chat's adopted set in line with what the page now knows, reading the stored set when the page first
 * looks and whenever a turn ends.
 *
 * The set read is adopted, open, when its reply is the chat's last and that reply's turn is done. An adopted set
 * collapses when a turn starts, and is dropped when that turn ends, when a different set is adopted, or when the
 * setting is off. A room has none.
 */
export function reconcileNextStepSuggestions(chatId: string, facts: NextStepFacts): void {
  const state = stateOf(chatId);
  const hasTurnEnded = state.wasTurnDone === false && facts.isTurnDone;
  state.wasTurnDone = facts.isTurnDone;
  if (facts.isRoom) {
    state.adopted = null;
    return;
  }
  if (hasTurnEnded) {
    // What was read before belongs to the reply before this turn.
    state.fetched = null;
    state.isReadOwed = true;
    void readNextStepSuggestions(chatId);
  } else if (state.isReadOwed && !state.isReading && Date.now() >= state.retryNotBefore) {
    void readNextStepSuggestions(chatId);
  }
  if (state.adopted !== null && !facts.isTurnDone && !state.adopted.hasTurnStartedSince) {
    state.adopted = { ...state.adopted, isOpen: false, hasTurnStartedSince: true };
  }
  if (!facts.isEnabled || (state.adopted !== null && state.adopted.hasTurnStartedSince && facts.isTurnDone)) {
    state.adopted = null;
  }
  const fetched = state.fetched;
  if (
    facts.isEnabled &&
    facts.isTurnDone &&
    fetched !== null &&
    fetched.after_event_id === facts.lastReplyEventId &&
    !isSameSet(state.adopted?.set, fetched)
  ) {
    state.adopted = { set: fetched, isOpen: true, hasTurnStartedSince: false };
  }
}

/** The set the chat's page draws, or null when it draws none. */
export function getNextStepSuggestions(chatId: string): AdoptedSuggestions | null {
  return stateByChat.get(chatId)?.adopted ?? null;
}

/** A message was sent in the chat: its open strip collapses to its toggle. */
export function collapseNextStepSuggestions(chatId: string): void {
  const state = stateByChat.get(chatId);
  if (state === undefined || state.adopted === null || !state.adopted.isOpen) return;
  state.adopted = { ...state.adopted, isOpen: false };
  m.redraw();
}

/** Open the chat's collapsed strip, or collapse its open one. */
export function toggleNextStepSuggestions(chatId: string): void {
  const state = stateByChat.get(chatId);
  if (state === undefined || state.adopted === null) return;
  state.adopted = { ...state.adopted, isOpen: !state.adopted.isOpen };
  m.redraw();
}

/** Forget every chat's suggestions (tests). */
export function resetSuggestionsForTests(): void {
  stateByChat.clear();
}
