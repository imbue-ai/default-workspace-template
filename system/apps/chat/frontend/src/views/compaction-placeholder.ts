/**
 * The inline row that stands at the end of the conversation while the chat's context is being
 * compacted, in the slot where the "Context was compacted" pill will land.
 *
 * It goes when the agent leaves COMPACTING, or as soon as this compaction's pill arrives on the
 * transcript, which can land a moment before the chat list reports the state change. The row
 * tells its pill apart by identity, not time: it remembers the newest pill when it mounts, and
 * any other pill becoming the newest is this compaction's.
 *
 * CLEANUP: once design review picks a compaction status presentation, delete this view if it
 * picks the activity strip.
 */

import m from "mithril";
import { activityDotClass } from "@imbue/workspace-ui/src/components/activityDot";
import { isCompactionStatusShownIn } from "../models/ChatSettings";
import { COMPACTING_STATE } from "../models/activityState";
import { getChatById, getQueuedMessagesForChat } from "../models/Chats";
import type { TranscriptEvent } from "../models/Response";
import { compactingLabel } from "./ActivityIndicator";
import { isStatusUserMessage } from "./message-classification";

export function newestCompactionPillId(events: readonly TranscriptEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (event.type === "user_message" && isStatusUserMessage(event)) return event.event_id;
  }
  return null;
}

interface CompactionPlaceholderAttrs {
  chatId: string;
  events: readonly TranscriptEvent[];
}

function CompactionPlaceholder(initial: m.Vnode<CompactionPlaceholderAttrs>): m.Component<CompactionPlaceholderAttrs> {
  // Mounted for as long as the agent stays COMPACTING, so a different newest pill is this
  // compaction's own.
  const pillIdAtMount = newestCompactionPillId(initial.attrs.events);
  return {
    view(vnode) {
      const { chatId, events } = vnode.attrs;
      if (newestCompactionPillId(events) !== pillIdAtMount) return null;
      const hasQueuedMessages = getQueuedMessagesForChat(chatId).length > 0;
      return m(
        "div",
        { class: "compaction-placeholder my-3 flex justify-center", role: "status", "aria-live": "polite" },
        [
          m(
            "span",
            {
              class:
                "inline-flex items-center gap-2 rounded-[12px] bg-fill-hover px-3 py-[3px] " +
                "text-(length:--font-size-helper) text-faint",
            },
            [
              m("span", { class: `compaction-placeholder__dot ${activityDotClass("h-1.5 w-1.5")}` }),
              m("span", { class: "compaction-placeholder__label" }, compactingLabel(hasQueuedMessages)),
            ],
          ),
        ],
      );
    },
  };
}

/**
 * The placeholder row for the end of the conversation, or null when the chat is not compacting.
 * Keyed, for the message list it sits in.
 */
export function renderCompactionPlaceholder(chatId: string, events: readonly TranscriptEvent[]): m.Children {
  if (getChatById(chatId)?.active_agent.activity_state !== COMPACTING_STATE) return null;
  // CLEANUP: once design review picks a compaction status presentation, drop this check.
  if (!isCompactionStatusShownIn("placeholder")) return null;
  return m(CompactionPlaceholder, { key: "compaction-placeholder", chatId, events });
}
