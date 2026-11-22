/**
 * Suggestions: short entries the user can click instead of typing (the backend's ``suggestions.py``).
 *
 * A chat's next-step suggestions are sets, each offered under one assistant message
 * (``GET /api/chats/<chat-id>/suggestions``): the stored set, which is the last reply's, and the sets the chat app
 * remembers of the replies before it. This module reads them and keeps, per chat, every set it knows by the reply
 * it follows, and which of them is drawn where. The set of the reply the chat waits on its user to answer is
 * adopted: drawn above the composer, behind a line that shows and hides it. It is adopted only for the reply it
 * names, once that reply's turn is done, so a set left over from an earlier reply is never drawn there: after a
 * reload, after a reply that arrived while the page was away, or on a second device. Every other set the page
 * knows is drawn in the transcript, under its reply.
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

/** A suggestion as the chat app sends it. An ``action`` marks one whose click does something other than send its
 *  label. */
type AnsweredSuggestion = Suggestion & { action?: string | null };

/** One set as the chat app sends it: the suggestions offered under the assistant message ``after_event_id``. */
export interface AnsweredSuggestionSet {
  after_event_id: string;
  source: string | null;
  suggestions: readonly AnsweredSuggestion[];
}

/** What ``GET /api/chats/<chat-id>/suggestions`` answers: ``ready`` with the stored set, ``pending`` while one is being
 *  worked out (a ``suggestions_updated`` push follows when one is stored), or ``none``; and in ``sets`` every set the
 *  chat app remembers for the chat, the stored one included. */
export interface SuggestionsAnswer {
  status: string;
  after_event_id: string | null;
  source: string | null;
  suggestions: readonly AnsweredSuggestion[];
  sets: readonly AnsweredSuggestionSet[];
}

/**
 * A set the chat app sent as the page draws it, or null when nothing of it is left to draw. A suggestion with an
 * action is dropped, since a next-step set carries none, so a newer backend never produces a dead pill; and only the
 * first suggestion may be the recommended one.
 */
export function drawableSetOf(answered: AnsweredSuggestionSet): SuggestionSet | null {
  const suggestions = answered.suggestions
    .filter((suggestion) => suggestion.action === undefined || suggestion.action === null)
    .map((suggestion, index) => ({
      suggestion_id: suggestion.suggestion_id,
      label: suggestion.label,
      is_recommended: suggestion.is_recommended && index === 0,
    }));
  if (suggestions.length === 0) return null;
  const source = answered.source === "fixed" || answered.source === "generated" ? answered.source : null;
  return { after_event_id: answered.after_event_id, source, suggestions };
}

/** The stored set an answer carries, or null when it carries none to draw: any status but "ready" (one this page
 *  does not know included), or a set with nothing left in it. */
export function suggestionSetOf(answer: SuggestionsAnswer): SuggestionSet | null {
  if (answer.status !== "ready" || answer.after_event_id === null) return null;
  return drawableSetOf({
    after_event_id: answer.after_event_id,
    source: answer.source,
    suggestions: answer.suggestions,
  });
}

/** A set as the page draws it: behind its line, or with its pills shown. */
export interface DrawnSuggestions {
  set: SuggestionSet;
  // Its pills are shown, rather than waiting behind its line.
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
  // The stored set the last answer carried.
  fetched: SuggestionSet | null;
  // Every set the page has been sent for the chat, by the event id of the reply it follows.
  knownByReply: Map<string, SuggestionSet>;
  // The set drawn above the composer.
  adopted: DrawnSuggestions | null;
  // The replies whose set is shown in the transcript, rather than waiting behind its line there.
  openAnsweredReplies: Set<string>;
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
      knownByReply: new Map(),
      adopted: null,
      openAnsweredReplies: new Set(),
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

/** Take in the sets an answer carries: the stored one, and each one the chat app remembers, which replaces what the
 *  page knew for its reply. A set the page knows and the answer leaves out stays known. */
function learnSets(state: ChatSuggestionState, answer: SuggestionsAnswer): void {
  state.fetched = suggestionSetOf(answer);
  for (const answered of answer.sets) {
    const set = drawableSetOf(answered);
    if (set === null) state.knownByReply.delete(answered.after_event_id);
    else state.knownByReply.set(set.after_event_id, set);
  }
  // Known whether or not the answer lists it among the remembered ones.
  if (state.fetched !== null) state.knownByReply.set(state.fetched.after_event_id, state.fetched);
}

/** Read the chat's sets. A read that fails is warned about and keeps what was read before; one the page still
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
    learnSets(state, answer);
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

/** Whether ``eventId`` is the reply the chat waits on its user to answer, which is where a set is drawn above the
 *  composer rather than in the transcript. */
function isReplyAwaited(facts: NextStepFacts, eventId: string): boolean {
  return facts.isTurnDone && facts.lastReplyEventId === eventId;
}

/**
 * Bring the chat's adopted set in line with what the page now knows, reading the chat's sets when the page first
 * looks and whenever a turn ends.
 *
 * The stored set is adopted while its reply is the one the chat waits on its user to answer: a fixed set with its
 * pills shown, a generated one behind its line. It stops being adopted the moment a turn starts or somebody speaks,
 * which is when it is drawn in the transcript instead (``getAnsweredReplySuggestions``), and while the setting is
 * off. A room has none.
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
  const fetched = state.fetched;
  if (!facts.isEnabled || fetched === null || !isReplyAwaited(facts, fetched.after_event_id)) {
    state.adopted = null;
    return;
  }
  if (isSameSet(state.adopted?.set, fetched)) return;
  state.adopted = { set: fetched, isOpen: fetched.source === "fixed" };
  // Should the reply be answered later, its set starts behind its line in the transcript.
  state.openAnsweredReplies.delete(fetched.after_event_id);
}

/** The set the chat's page draws above its composer, or null when it draws none there. */
export function getNextStepSuggestions(chatId: string): DrawnSuggestions | null {
  return stateByChat.get(chatId)?.adopted ?? null;
}

/** Hide the pills of the set above the composer behind its line, because another strip opened. */
export function collapseNextStepSuggestions(chatId: string): void {
  const state = stateByChat.get(chatId);
  if (state === undefined || state.adopted === null || !state.adopted.isOpen) return;
  state.adopted = { ...state.adopted, isOpen: false };
  m.redraw();
}

/** Show the pills of the set above the composer, or hide them behind its line. */
export function toggleNextStepSuggestions(chatId: string): void {
  const state = stateByChat.get(chatId);
  if (state === undefined || state.adopted === null) return;
  state.adopted = { ...state.adopted, isOpen: !state.adopted.isOpen };
  m.redraw();
}

/**
 * The set the page draws in the transcript under the reply ``eventId``, or null when it draws none there: the page
 * knows no set for the reply, the reply is the one the chat waits on its user to answer (its set belongs above the
 * composer), the chat is a room, or the setting is not known to be on. ``readFacts`` is asked only for a reply the
 * page knows a set for, which few of a transcript's messages are.
 */
export function getAnsweredReplySuggestions(
  chatId: string,
  eventId: string,
  readFacts: () => NextStepFacts,
): DrawnSuggestions | null {
  const state = stateByChat.get(chatId);
  const set = state?.knownByReply.get(eventId);
  if (state === undefined || set === undefined) return null;
  const facts = readFacts();
  if (!facts.isEnabled || facts.isRoom || isReplyAwaited(facts, eventId)) return null;
  return { set, isOpen: state.openAnsweredReplies.has(eventId) };
}

/** Show the pills of the set under the answered reply ``eventId``, or hide them behind its line. */
export function toggleAnsweredReplySuggestions(chatId: string, eventId: string): void {
  const state = stateByChat.get(chatId);
  if (state === undefined || !state.knownByReply.has(eventId)) return;
  if (!state.openAnsweredReplies.delete(eventId)) state.openAnsweredReplies.add(eventId);
  m.redraw();
}

/** Forget every chat's suggestions (tests). */
export function resetSuggestionsForTests(): void {
  stateByChat.clear();
}
