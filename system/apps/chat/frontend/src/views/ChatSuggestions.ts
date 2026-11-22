/**
 * The suggestions above the composer: the strips of the three placements, drawn together. A reply's suggested
 * answers ("Next steps", ``views/NextStepSuggestions.ts``), and things worth trying in Studio while the agent works
 * ("While you wait") and on a chat nobody has spoken in ("Things to try", ``views/ActionSuggestions.ts``).
 *
 * At most one strip is open: the one that opened last, and the others collapse to their toggles, which sit together
 * in one row over it. Each strip's link switches its own placement off for the whole workspace
 * (``views/suggestion-placement.ts``), which only the workspace's owner is offered.
 */

import m from "mithril";
import { ensureChatSettings, isSuggestionSwitchOn } from "../models/ChatSettings";
import { isViewerOwner } from "../models/Participants";
import { SUGGESTION_SWITCHES } from "../models/Suggestions";
import type { SuggestionPlacement } from "../models/Suggestions";
import { actionStrips } from "./ActionSuggestions";
import { followNextStepSuggestions, nextStepStrip } from "./NextStepSuggestions";
import { makeSuggestionSwitchLink } from "./suggestion-placement";
import type { PlacementStrip, SuggestionSwitchLink } from "./suggestion-placement";
import { SuggestionStrip, suggestionsOffNotice } from "./SuggestionStrip";

// The order the placements are drawn in, which is also which strip stays open should two open in one draw: the
// reply's own suggestions before anything else, then what the turn under way offers.
const PLACEMENTS: readonly SuggestionPlacement[] = ["next_step", "activity", "new_chat"];

// The composer's own column, so the pills line up with its edge. The toggles share its first row, and the open
// strip takes a row of its own under them.
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
  let openAtLastDraw: ReadonlySet<SuggestionPlacement> = new Set();
  const links: Record<SuggestionPlacement, SuggestionSwitchLink> = {
    next_step: makeSuggestionSwitchLink("next_step"),
    activity: makeSuggestionSwitchLink("activity"),
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
      const nextStep = nextStepStrip(chatId, isPlacementEnabled("next_step"));
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
        if (strip === undefined) continue;
        children.push(
          m(SuggestionStrip, {
            key: `${placement}:${strip.setKey}`,
            placement,
            suggestions: strip.suggestions,
            foldAfter: strip.foldAfter,
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
