/**
 * The suggestions above the composer: the strips of the two placements. Things worth trying in Studio on a chat
 * nobody has spoken in ("Things to try", ``views/NewChatStrip.ts``), and the suggested answers to the reply the
 * chat waits on (``views/NextStepSuggestions.ts``).
 *
 * A strip is its pills over one quiet line, and the column holds exactly one such line directly above the
 * composer in every chat with an agent, with or without anything to say, so a strip that comes or goes moves
 * nothing; only next-step suggestions known to be switched off take it away.
 *
 * The two strips are never there together: a chat nobody has spoken in has no reply to suggest answers to, and
 * the first-run chat, whose first strip is its tree's, is offered no new-chat suggestions. Should both ever be,
 * the next-step strip is the one drawn. Each strip's link switches its own placement off for the whole workspace
 * (``views/suggestion-placement.ts``), which only the workspace's owner is offered.
 */

import m from "mithril";
import { ensureChatSettings, isSuggestionSwitchOn } from "../models/ChatSettings";
import { isRoomChat } from "../models/Chats";
import { isViewerOwner } from "../models/Participants";
import { SUGGESTION_SWITCHES } from "../models/Suggestions";
import type { SuggestionPlacement } from "../models/Suggestions";
import { newChatStrip } from "./NewChatStrip";
import { followNextStepSuggestions, isNextStepSwitchedOff, nextStepStrip } from "./NextStepSuggestions";
import { makeSuggestionSwitchLink } from "./suggestion-placement";
import type { PlacementStrip, SuggestionSwitchLink } from "./suggestion-placement";
import { emptyNextStepLine, SuggestionStrip, suggestionsOffNotice } from "./SuggestionStrip";

// The order the placements are drawn in: the next-step strip last, so its line is the one directly above the
// composer.
const PLACEMENTS: readonly SuggestionPlacement[] = ["new_chat", "next_step"];

// The composer's own column, so the strips line up with its edge.
const COLUMN_CLASS = "chat-suggestions mx-auto mb-2 flex w-full max-w-(--width-message-column) flex-col gap-1";

function isPlacementEnabled(placement: SuggestionPlacement): boolean {
  return isSuggestionSwitchOn(SUGGESTION_SWITCHES[placement]) === true;
}

export function ChatSuggestions(): m.Component<{ chatId: string }> {
  let shownChatId: string | null = null;
  let stopFollowingNextStep: () => void = () => undefined;
  const links: Record<SuggestionPlacement, SuggestionSwitchLink> = {
    next_step: makeSuggestionSwitchLink("next_step"),
    new_chat: makeSuggestionSwitchLink("new_chat"),
  };

  return {
    oninit() {
      stopFollowingNextStep = followNextStepSuggestions(() => shownChatId);
    },

    onremove() {
      stopFollowingNextStep();
      for (const placement of PLACEMENTS) links[placement].dispose();
    },

    view(vnode) {
      const chatId = vnode.attrs.chatId;
      shownChatId = chatId;
      void ensureChatSettings();
      const nextStep = nextStepStrip(chatId);
      // Read on every draw, so the chat's first message is seen whichever strip is drawn.
      const newChat = newChatStrip(chatId, isPlacementEnabled("new_chat"));
      const strips: Record<SuggestionPlacement, PlacementStrip | null> = {
        new_chat: nextStep === null ? newChat : null,
        next_step: nextStep,
      };
      // A new-chat strip's own line stands where the next-step line would, at the same height.
      const isEmptyLineKept = !isRoomChat(chatId) && !isNextStepSwitchedOff() && strips.new_chat === null;
      const isOwner = isViewerOwner();
      const children: m.Children[] = [];
      for (const placement of PLACEMENTS) {
        const link = links[placement];
        if (link.isOffNoticeShown(isPlacementEnabled(placement))) {
          children.push(m.fragment({ key: `${placement}:off` }, [suggestionsOffNotice(placement, link.undo)]));
          continue;
        }
        const strip = strips[placement];
        if (strip === null) {
          if (placement === "next_step" && isEmptyLineKept) {
            children.push(m.fragment({ key: "next_step:empty" }, [emptyNextStepLine()]));
          }
          continue;
        }
        children.push(
          m(SuggestionStrip, {
            key: `${placement}:${strip.setKey}`,
            placement,
            suggestions: strip.suggestions,
            isOpen: strip.isOpen,
            onPick: strip.onPick,
            onToggle: strip.onToggle,
            onDisable: isOwner ? link.disable : null,
          }),
        );
      }
      if (children.length === 0) return null;
      return m("div", { class: COLUMN_CLASS }, children);
    },
  };
}
