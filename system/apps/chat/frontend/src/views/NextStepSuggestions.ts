/**
 * The next-step strip above the composer: the suggested replies to the agent's last reply (``models/Suggestions.ts``
 * decides which set is drawn, from what this view reads of the chat on every draw).
 *
 * A click on a suggestion sends it through the composer. The strip's link switches next-step suggestions off for the
 * whole workspace, which only the workspace's owner is offered: the strip goes, a line saying so takes its place for
 * a few seconds with a way back, and an Imbue Studio chrome that opens its workspace settings is asked to.
 */

import m from "mithril";
import { ensureChatSettings, isSuggestionSwitchOn, updateSuggestionSwitches } from "../models/ChatSettings";
import { getChatById, getProvisionalChat, isLaunchInFlight, isRoomChat } from "../models/Chats";
import { getOutgoingMessages } from "../models/OutgoingMessages";
import { isViewerOwner } from "../models/Participants";
import { addMessageSentListener, getEventsForChat, hasMoreAfter, removeMessageSentListener } from "../models/Response";
import {
  collapseNextStepSuggestions,
  getNextStepSuggestions,
  reconcileNextStepSuggestions,
  SUGGESTION_SWITCHES,
  toggleNextStepSuggestions,
} from "../models/Suggestions";
import { openWorkspaceSettings, opensWorkspaceSettings } from "../shell";
import { isNonBoundaryUserMessage } from "./message-classification";
import { submitSuggestion } from "./MessageInput";
import { SuggestionStrip, suggestionsOffNotice } from "./SuggestionStrip";
import { isChatTurnOver } from "./TurnDoneMark";

const NEXT_STEP_SWITCH = SUGGESTION_SWITCHES.next_step;
// The group of the chrome's workspace settings that holds the suggestion switches.
const SUGGESTIONS_SETTINGS_GROUP = "suggestions";
export const SUGGESTIONS_OFF_NOTICE_MS = 8_000;

/** Whether the chat's turn is done, as the done mark reads it. A chat still awaiting its first send (a seeded one,
 *  before its first agent) has no turn to be in, and counts as done until something is sent. */
export function isTurnDoneForSuggestions(chatId: string): boolean {
  if (isChatTurnOver(chatId)) return true;
  const provisional = getProvisionalChat(chatId);
  return (
    provisional !== undefined &&
    provisional.phase === "awaiting_first_send" &&
    getChatById(chatId) === undefined &&
    !isLaunchInFlight(chatId) &&
    getOutgoingMessages(chatId).length === 0
  );
}

/** The event id of the reply a set of suggestions may follow: the last assistant message the page holds, when the
 *  page holds the transcript's end and nobody has said anything since. */
export function lastReplyEventId(chatId: string): string | null {
  if (hasMoreAfter(chatId)) return null;
  const events = getEventsForChat(chatId);
  for (let index = events.length - 1; index >= 0; index--) {
    const event = events[index];
    if (event.type === "assistant_message") return event.event_id;
    if (event.type === "user_message" && !isNonBoundaryUserMessage(event)) return null;
  }
  return null;
}

export function NextStepSuggestions(): m.Component<{ chatId: string }> {
  let currentChatId: string | null = null;
  let isOffNoticeShown = false;
  let offNoticeTimer: ReturnType<typeof setTimeout> | null = null;

  function hideOffNotice(): void {
    isOffNoticeShown = false;
    if (offNoticeTimer !== null) clearTimeout(offNoticeTimer);
    offNoticeTimer = null;
  }

  function disable(): void {
    // The chrome's settings read the switches as they open, so they are opened only once the write has answered,
    // and not at all when it was refused.
    void updateSuggestionSwitches({ [NEXT_STEP_SWITCH]: false }).then(() => {
      if (isSuggestionSwitchOn(NEXT_STEP_SWITCH) === false && opensWorkspaceSettings()) {
        openWorkspaceSettings(SUGGESTIONS_SETTINGS_GROUP);
      }
    });
    hideOffNotice();
    isOffNoticeShown = true;
    offNoticeTimer = setTimeout(() => {
      hideOffNotice();
      m.redraw();
    }, SUGGESTIONS_OFF_NOTICE_MS);
  }

  function undo(): void {
    hideOffNotice();
    void updateSuggestionSwitches({ [NEXT_STEP_SWITCH]: true });
  }

  const collapseOnSend = (chatId: string): void => {
    if (chatId === currentChatId) collapseNextStepSuggestions(chatId);
  };

  return {
    oninit() {
      addMessageSentListener(collapseOnSend);
    },

    onremove() {
      removeMessageSentListener(collapseOnSend);
      hideOffNotice();
    },

    view(vnode) {
      const chatId = vnode.attrs.chatId;
      currentChatId = chatId;
      void ensureChatSettings();
      const isEnabled = isSuggestionSwitchOn(NEXT_STEP_SWITCH) === true;
      reconcileNextStepSuggestions(chatId, {
        isEnabled,
        isRoom: isRoomChat(chatId),
        isTurnDone: isTurnDoneForSuggestions(chatId),
        lastReplyEventId: lastReplyEventId(chatId),
      });
      // Switched back on from somewhere else, or the write was refused: the line would be wrong.
      if (isOffNoticeShown && isEnabled) hideOffNotice();
      if (isOffNoticeShown) return suggestionsOffNotice("next_step", undo);
      const adopted = getNextStepSuggestions(chatId);
      if (adopted === null) return null;
      // Keyed by the reply the set follows, so the next reply's strip rises in rather than changing in place.
      return [
        m(SuggestionStrip, {
          key: adopted.set.after_event_id,
          placement: "next_step",
          suggestions: adopted.set.suggestions,
          isOpen: adopted.isOpen,
          onPick: (suggestion) => submitSuggestion(chatId, suggestion.label),
          onToggle: () => toggleNextStepSuggestions(chatId),
          onDisable: isViewerOwner() ? disable : null,
        }),
      ];
    },
  };
}
