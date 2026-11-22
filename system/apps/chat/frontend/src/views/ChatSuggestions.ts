/**
 * The suggestions above the composer: the strips of the three placements, drawn together. Things worth trying in
 * Studio while the agent works ("While you wait") and on a chat nobody has spoken in ("Things to try",
 * ``views/ActionSuggestions.ts``), and under them, directly above the composer, the line of the suggested answers
 * to the reply the chat waits on (``views/NextStepSuggestions.ts``).
 *
 * That line keeps its height in every chat with an agent, with or without a set to speak of, so a set that arrives
 * moves nothing; only next-step suggestions known to be switched off take it away.
 *
 * At most one strip has its pills shown: the one that opened last. An activity or a new-chat strip that gives way
 * collapses to its toggle, and the toggles sit together in one row over the open strip; the next-step strip's pills
 * go back behind its line. Each strip's link switches its own placement off for the whole workspace
 * (``views/suggestion-placement.ts``), which only the workspace's owner is offered.
 */

import m from "mithril";
import { ensureChatSettings, isSuggestionSwitchOn } from "../models/ChatSettings";
import { isRoomChat } from "../models/Chats";
import { isViewerOwner } from "../models/Participants";
import { SUGGESTION_SWITCHES } from "../models/Suggestions";
import type { SuggestionPlacement } from "../models/Suggestions";
import { actionStrips, followActionSuggestions } from "./ActionSuggestions";
import { followNextStepSuggestions, isNextStepSwitchedOff, nextStepStrip } from "./NextStepSuggestions";
import { makeSuggestionSwitchLink } from "./suggestion-placement";
import type { PlacementStrip, SuggestionSwitchLink } from "./suggestion-placement";
import { emptyNextStepLine, SuggestionStrip, suggestionsOffNotice } from "./SuggestionStrip";

// The order the placements are drawn in: the next-step strip last, so its line is the one directly above the
// composer.
const PLACEMENTS: readonly SuggestionPlacement[] = ["activity", "new_chat", "next_step"];

// The composer's own column, so the strips line up with its edge. The toggles share its first row, and an open
// strip and the next-step line each take a row of their own under them.
const COLUMN_CLASS =
  "chat-suggestions mx-auto mb-2 flex w-full max-w-(--width-message-column) flex-wrap items-center gap-x-1 gap-y-1";

function isPlacementEnabled(placement: SuggestionPlacement): boolean {
  return isSuggestionSwitchOn(SUGGESTION_SWITCHES[placement]) === true;
}

/**
 * Leave at most one of ``strips`` open: the one that was not open at the last draw, else the first. Answers the
 * strips as they are drawn, and collapses the others where they keep their state, so they stay collapsed.
 */
function keepOneOpen(
  strips: readonly PlacementStrip[],
  wasOpen: ReadonlySet<SuggestionPlacement>,
): readonly PlacementStrip[] {
  const open = strips.filter((strip) => strip.isOpen);
  if (open.length <= 1) return strips;
  const kept = open.find((strip) => !wasOpen.has(strip.placement)) ?? open[0];
  return strips.map((strip) => {
    if (!strip.isOpen || strip === kept) return strip;
    strip.collapse();
    return { ...strip, isOpen: false };
  });
}

export function ChatSuggestions(): m.Component<{ chatId: string }> {
  let shownChatId: string | null = null;
  let stopFollowingNextStep: () => void = () => undefined;
  let stopFollowingActions: () => void = () => undefined;
  let openAtLastDraw: ReadonlySet<SuggestionPlacement> = new Set();
  const links: Record<SuggestionPlacement, SuggestionSwitchLink> = {
    next_step: makeSuggestionSwitchLink("next_step"),
    activity: makeSuggestionSwitchLink("activity"),
    new_chat: makeSuggestionSwitchLink("new_chat"),
  };

  return {
    oninit() {
      stopFollowingNextStep = followNextStepSuggestions(() => shownChatId);
      stopFollowingActions = followActionSuggestions(() => shownChatId);
    },

    onremove() {
      stopFollowingNextStep();
      stopFollowingActions();
      for (const placement of PLACEMENTS) links[placement].dispose();
    },

    view(vnode) {
      const chatId = vnode.attrs.chatId;
      shownChatId = chatId;
      void ensureChatSettings();
      const nextStep = nextStepStrip(chatId);
      // In the order that says which strip stays open should two open in one draw: the reply's own suggestions
      // before anything else, then what the turn under way offers.
      const strips = keepOneOpen(
        [
          ...(nextStep === null ? [] : [nextStep]),
          ...actionStrips(chatId, {
            activity: isPlacementEnabled("activity"),
            new_chat: isPlacementEnabled("new_chat"),
          }),
        ],
        openAtLastDraw,
      );
      openAtLastDraw = new Set(strips.filter((strip) => strip.isOpen).map((strip) => strip.placement));
      const isOwner = isViewerOwner();
      const children: m.Children[] = [];
      for (const placement of PLACEMENTS) {
        const link = links[placement];
        if (link.isOffNoticeShown(isPlacementEnabled(placement))) {
          children.push(m.fragment({ key: `${placement}:off` }, [suggestionsOffNotice(placement, link.undo)]));
          continue;
        }
        const strip = strips.find((entry) => entry.placement === placement);
        if (strip === undefined) {
          if (placement === "next_step" && !isRoomChat(chatId) && !isNextStepSwitchedOff()) {
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
