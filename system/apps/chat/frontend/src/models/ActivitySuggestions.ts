/**
 * Activity and new-chat suggestions: things worth trying in Studio that the workspace's owner has not done yet,
 * offered while the agent works on a reply (the activity strip) and on a chat nobody has said anything in (the
 * new-chat strip). Each is an action suggestion: a click does something other than send its label
 * (``views/suggestion-actions.ts``).
 *
 * The chat app decides which hold (``GET /api/suggestions/activity``, the backend's ``activity_suggestions.py``); it
 * answers nothing to anyone but the workspace's owner, before the tour has finished, or with the placement switched
 * off. This module reads the answer per chat and placement, and keeps, per chat, the two strips the page draws: each
 * the first few suggestions the page can act on, open or collapsed to its toggle.
 *
 * The activity strip opens when a turn starts, with a fresh read, and collapses when the turn ends. The new-chat
 * strip is open on a chat with no user message, collapses on the chat's first message, and is dropped for good when
 * the activity strip shows the same suggestions, so the two are never on screen together.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { addChatSettingsUpdatedListener } from "./ChatSettings";
import type { ActionSuggestion, SuggestionAction, SuggestionPlacement } from "./Suggestions";

/** The placements whose suggestions are actions. */
export type ActionPlacement = Exclude<SuggestionPlacement, "next_step">;

const ACTION_PLACEMENTS: readonly ActionPlacement[] = ["activity", "new_chat"];

/** How many suggestions a strip shows, of those the page can act on. */
export const MAX_ACTION_SUGGESTIONS_SHOWN = 3;
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

/** What ``GET /api/suggestions/activity`` answers: every suggestion whose condition holds, in a fixed order. */
export interface ActivityAnswer {
  suggestions: readonly { suggestion_id: string; label: string; action?: string | null }[];
}

/** The suggestions of an answer this page can draw: those whose action it knows. One with no action, or with an
 *  action a newer backend added, is dropped, so no pill is ever dead. */
export function actionSuggestionsOf(answer: ActivityAnswer): ActionSuggestion[] {
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

/** What the page knows of the chat when it decides which strips to draw. */
export interface ActivityFacts {
  isRoom: boolean;
  // Whether no reply is being worked on in the chat; null while the page cannot tell (the chat or its transcript
  // has not arrived).
  isTurnDone: boolean | null;
  // Whether anybody has said anything in the chat; null while the page cannot tell.
  hasUserMessage: boolean | null;
  // Whether each placement is switched on (known to be: false while the settings load).
  isEnabled: Record<ActionPlacement, boolean>;
  // Whether this page can do what a click on the suggestion asks for.
  canActOn: (suggestion: ActionSuggestion) => boolean;
}

/** A strip the page draws. */
export interface ActionStrip {
  suggestions: readonly ActionSuggestion[];
  // Drawn as its pills, rather than collapsed to its toggle.
  isOpen: boolean;
}

interface PlacementRead {
  // What the last answer carried; null before one, and from a turn's start until that turn's read answers.
  suggestions: readonly ActionSuggestion[] | null;
  // Counts the reads asked for, so an answer that a later read has overtaken is dropped.
  latestRead: number;
  isReading: boolean;
  // A read is wanted and none has answered yet.
  isReadOwed: boolean;
  // After a failed read, when the page's looks may ask again: they come on every render.
  retryNotBefore: number;
}

// The new-chat strip of a chat: not decided before the page knows whether the chat has a message, then open or
// collapsed on a chat that had none when the page first looked, and absent on any other, or once dropped.
type NewChatPhase = "undecided" | "open" | "collapsed" | "absent";

interface ChatActivityState {
  reads: Record<ActionPlacement, PlacementRead>;
  // Whether the turn was done at the last look that could tell; null before one.
  wasTurnDone: boolean | null;
  // The activity strip of the turn the page last saw start; null before it saw any.
  activity: { isOpen: boolean } | null;
  newChat: NewChatPhase;
  // Whether a message has been seen in a chat that had none when the page first looked.
  isFirstMessageSeen: boolean;
  drawn: Record<ActionPlacement, ActionStrip | null>;
}

const stateByChat = new Map<string, ChatActivityState>();

function newRead(): PlacementRead {
  return { suggestions: null, latestRead: 0, isReading: false, isReadOwed: false, retryNotBefore: 0 };
}

function stateOf(chatId: string): ChatActivityState {
  let state = stateByChat.get(chatId);
  if (state === undefined) {
    state = {
      reads: { activity: newRead(), new_chat: newRead() },
      wasTurnDone: null,
      activity: null,
      newChat: "undecided",
      isFirstMessageSeen: false,
      drawn: { activity: null, new_chat: null },
    };
    stateByChat.set(chatId, state);
  }
  return state;
}

/** Ask for a read whatever stands in its way: one already out is overtaken, and a failed one's wait is over. */
function oweRead(read: PlacementRead): void {
  read.latestRead++;
  read.isReading = false;
  read.isReadOwed = true;
  read.retryNotBefore = 0;
}

let isFollowingChatSettings = false;

/** A switch changed somewhere: what the route answers depends on it, so every placement read so far is read
 *  again. Registered on the first look rather than as this module loads. */
function followChatSettings(): void {
  if (isFollowingChatSettings) return;
  isFollowingChatSettings = true;
  addChatSettingsUpdatedListener(() => {
    for (const state of stateByChat.values()) {
      for (const placement of ACTION_PLACEMENTS) {
        const read = state.reads[placement];
        if (read.suggestions !== null || read.isReadOwed) oweRead(read);
      }
    }
  });
}

/** Read what the chat app offers ``chatId`` for ``placement``. A read that fails is warned about and keeps what was
 *  read before; one the page still owes is asked for again by a later look, after ``RETRY_DELAY_MS``. */
export async function readActivitySuggestions(chatId: string, placement: ActionPlacement): Promise<void> {
  const read = stateOf(chatId).reads[placement];
  const attempt = ++read.latestRead;
  read.isReading = true;
  try {
    const answer = await m.request<ActivityAnswer>({
      method: "GET",
      url: apiUrl("/api/suggestions/activity"),
      params: { placement, chat_id: chatId },
    });
    if (attempt !== read.latestRead) return;
    read.suggestions = actionSuggestionsOf(answer);
    read.isReadOwed = false;
  } catch (error) {
    if (attempt !== read.latestRead) return;
    console.warn(`Failed to read the ${placement} suggestions of chat ${chatId}`, error);
    read.retryNotBefore = Date.now() + RETRY_DELAY_MS;
  } finally {
    if (attempt === read.latestRead) read.isReading = false;
  }
  m.redraw();
}

/**
 * An action suggestion was clicked: tell the chat app, which retires the suggestions a click retires, and read
 * again what it offers this chat. A click the chat app could not be told of is warned about, and the read still
 * follows.
 */
export async function recordActionSuggestionClicked(chatId: string, suggestionId: string): Promise<void> {
  try {
    await m.request<unknown>({
      method: "POST",
      url: apiUrl("/api/suggestions/activity/clicked"),
      body: { suggestion_id: suggestionId },
    });
  } catch (error) {
    console.warn(`Failed to record the click on suggestion ${suggestionId}`, error);
  }
  const state = stateByChat.get(chatId);
  if (state === undefined) return;
  await Promise.all(
    ACTION_PLACEMENTS.filter((placement) => state.reads[placement].suggestions !== null).map((placement) =>
      readActivitySuggestions(chatId, placement),
    ),
  );
}

function followTurn(state: ChatActivityState, isTurnDone: boolean | null): void {
  if (isTurnDone === null) return;
  const hasTurnStarted = !isTurnDone && state.wasTurnDone !== false;
  const hasTurnEnded = isTurnDone && state.wasTurnDone === false;
  state.wasTurnDone = isTurnDone;
  if (hasTurnStarted) {
    state.activity = { isOpen: true };
    // What was read before belongs to the turn before this one.
    state.reads.activity.suggestions = null;
    oweRead(state.reads.activity);
  }
  if (hasTurnEnded && state.activity !== null) state.activity = { isOpen: false };
}

function followFirstMessage(state: ChatActivityState, hasUserMessage: boolean | null): void {
  if (hasUserMessage === null) return;
  if (state.newChat === "undecided") {
    state.newChat = hasUserMessage ? "absent" : "open";
    if (!hasUserMessage) oweRead(state.reads.new_chat);
    return;
  }
  if (!hasUserMessage || state.isFirstMessageSeen) return;
  state.isFirstMessageSeen = true;
  if (state.newChat === "open") state.newChat = "collapsed";
}

function shownOf(read: PlacementRead, isEnabled: boolean, facts: ActivityFacts): readonly ActionSuggestion[] {
  if (!isEnabled || read.suggestions === null) return [];
  return read.suggestions.filter(facts.canActOn).slice(0, MAX_ACTION_SUGGESTIONS_SHOWN);
}

function isSameSuggestions(left: readonly ActionSuggestion[], right: readonly ActionSuggestion[]): boolean {
  return (
    left.length === right.length && left.every((suggestion, i) => suggestion.suggestion_id === right[i].suggestion_id)
  );
}

/**
 * Bring the chat's two strips in line with what the page now knows, reading what the chat app offers when a turn
 * starts and when the page first looks at a chat with no message.
 *
 * A strip is drawn only with something to show: the first ``MAX_ACTION_SUGGESTIONS_SHOWN`` of what was read that
 * the page can act on, while its placement is switched on. A room has none.
 */
export function reconcileActivitySuggestions(chatId: string, facts: ActivityFacts): void {
  followChatSettings();
  const state = stateOf(chatId);
  if (facts.isRoom) {
    state.drawn = { activity: null, new_chat: null };
    return;
  }
  followTurn(state, facts.isTurnDone);
  followFirstMessage(state, facts.hasUserMessage);
  for (const placement of ACTION_PLACEMENTS) {
    const read = state.reads[placement];
    if (read.isReadOwed && facts.isEnabled[placement] && !read.isReading && Date.now() >= read.retryNotBefore) {
      void readActivitySuggestions(chatId, placement);
    }
  }
  const activity = state.activity === null ? [] : shownOf(state.reads.activity, facts.isEnabled.activity, facts);
  let newChat =
    state.newChat === "open" || state.newChat === "collapsed"
      ? shownOf(state.reads.new_chat, facts.isEnabled.new_chat, facts)
      : [];
  if (state.isFirstMessageSeen && newChat.length > 0) {
    // The read of the turn the first message opened is still out, so whether the activity strip is about to
    // show the same suggestions is not known yet: nothing is drawn in the new-chat strip's place meanwhile.
    const isActivityReadAwaited =
      state.activity !== null &&
      facts.isEnabled.activity &&
      state.reads.activity.suggestions === null &&
      Date.now() >= state.reads.activity.retryNotBefore;
    if (activity.length > 0 && isSameSuggestions(activity, newChat)) {
      state.newChat = "absent";
      newChat = [];
    } else if (isActivityReadAwaited) {
      newChat = [];
    }
  }
  state.drawn = {
    activity: state.activity === null || activity.length === 0 ? null : { suggestions: activity, ...state.activity },
    new_chat: newChat.length === 0 ? null : { suggestions: newChat, isOpen: state.newChat === "open" },
  };
}

/** The strip of ``placement`` the chat's page draws, as of the last ``reconcileActivitySuggestions``, or null. */
export function getActionStrip(chatId: string, placement: ActionPlacement): ActionStrip | null {
  return stateByChat.get(chatId)?.drawn[placement] ?? null;
}

function setOpen(chatId: string, placement: ActionPlacement, isOpen: (wasOpen: boolean) => boolean): void {
  const state = stateByChat.get(chatId);
  if (state === undefined) return;
  switch (placement) {
    case "activity":
      if (state.activity !== null) state.activity = { isOpen: isOpen(state.activity.isOpen) };
      break;
    case "new_chat":
      if (state.newChat === "open" || state.newChat === "collapsed") {
        state.newChat = isOpen(state.newChat === "open") ? "open" : "collapsed";
      }
      break;
  }
  m.redraw();
}

/** Open the chat's collapsed strip of ``placement``, or collapse its open one. */
export function toggleActionStrip(chatId: string, placement: ActionPlacement): void {
  setOpen(chatId, placement, (wasOpen) => !wasOpen);
}

/** Collapse the chat's strip of ``placement`` to its toggle. */
export function collapseActionStrip(chatId: string, placement: ActionPlacement): void {
  setOpen(chatId, placement, () => false);
}

/** Forget every chat's activity and new-chat suggestions (tests). */
export function resetActivitySuggestionsForTests(): void {
  stateByChat.clear();
}
