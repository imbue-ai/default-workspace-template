/**
 * The next-step strip above the composer: the suggested replies to the agent's last reply (``models/Suggestions.ts``
 * decides which set is drawn, from what this view reads of the chat on every draw).
 *
 * The chat app says when the chat's stored set changes, and the strip reads it again. A click on a suggestion sends
 * it through the composer. The strip's link switches next-step suggestions off for the
 * whole workspace, which only the workspace's owner is offered: the strip goes, a line saying so takes its place for
 * a few seconds with a way back, and an Imbue Studio chrome that opens its workspace settings is asked to.
 */

import m from "mithril";
import { ensureChatSettings, isSuggestionSwitchOn, updateSuggestionSwitches } from "../models/ChatSettings";
import { addSuggestionsUpdatedListener, isRoomChat, removeSuggestionsUpdatedListener } from "../models/Chats";
import { isViewerOwner } from "../models/Participants";
import { addMessageSentListener, removeMessageSentListener } from "../models/Response";
import {
  collapseNextStepSuggestions,
  getNextStepSuggestions,
  reconcileNextStepSuggestions,
  rereadNextStepSuggestions,
  SUGGESTION_SWITCHES,
  toggleNextStepSuggestions,
} from "../models/Suggestions";
import { openWorkspaceSettings, opensWorkspaceSettings } from "../shell";
import { isTurnDoneForSuggestions, lastReplyEventId } from "./last-reply";
import { submitSuggestion } from "./MessageInput";
import { SuggestionStrip, suggestionsOffNotice } from "./SuggestionStrip";

const NEXT_STEP_SWITCH = SUGGESTION_SWITCHES.next_step;
// How many of a generated set's suggestions are drawn before "Show more". A fixed set is drawn whole.
const GENERATED_SET_FOLD = 3;
// The group of the chrome's workspace settings that holds the suggestion switches.
const SUGGESTIONS_SETTINGS_GROUP = "suggestions";
export const SUGGESTIONS_OFF_NOTICE_MS = 8_000;

export function NextStepSuggestions(): m.Component<{ chatId: string }> {
  let currentChatId: string | null = null;
  let isOffNoticeShown = false;
  let offNoticeTimer: ReturnType<typeof setTimeout> | null = null;
  // How many times the link or Undo has changed the switch, so a write can tell whether it is still the latest.
  let switchChangeCount = 0;

  function hideOffNotice(): void {
    isOffNoticeShown = false;
    if (offNoticeTimer !== null) clearTimeout(offNoticeTimer);
    offNoticeTimer = null;
  }

  function disable(): void {
    const change = ++switchChangeCount;
    // The chrome's settings read the switches as they open, so they are opened only once the write has answered,
    // and not at all when it was refused or Undo was clicked while it was out.
    void updateSuggestionSwitches({ [NEXT_STEP_SWITCH]: false }).then(() => {
      if (change !== switchChangeCount) return;
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
    switchChangeCount++;
    hideOffNotice();
    void updateSuggestionSwitches({ [NEXT_STEP_SWITCH]: true });
  }

  const collapseOnSend = (chatId: string): void => {
    if (chatId === currentChatId) collapseNextStepSuggestions(chatId);
  };

  // A set worked out since the turn ended arrives this way, seconds after the read the turn's end asked for.
  const rereadOnPush = (chatId: string): void => {
    if (chatId === currentChatId) rereadNextStepSuggestions(chatId);
  };

  return {
    oninit() {
      addMessageSentListener(collapseOnSend);
      addSuggestionsUpdatedListener(rereadOnPush);
    },

    onremove() {
      removeMessageSentListener(collapseOnSend);
      removeSuggestionsUpdatedListener(rereadOnPush);
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
          foldAfter: adopted.set.source === "fixed" ? null : GENERATED_SET_FOLD,
          isOpen: adopted.isOpen,
          onPick: (suggestion) => submitSuggestion(chatId, suggestion.label),
          onToggle: () => toggleNextStepSuggestions(chatId),
          onDisable: isViewerOwner() ? disable : null,
        }),
      ];
    },
  };
}
