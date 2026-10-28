/**
 * "Done in 14s", with a check, under the latest reply once the agent has finished the turn: the
 * explicit end the status row above it hands over to, so a finished chat never reads as one that
 * might still be going.
 *
 * It shows as soon as the agent's turn is over -- the backend reports its activity idle, nothing
 * queued or connecting, no send of this page's on the way -- even while its harness still closes the
 * turn (Claude's Stop hooks, the chat still reading working): the reply is in, so the user's wait is
 * over. A hook that sends the agent back to work takes the mark away again with the activity it
 * reports. It never shows while the chat waits on a background task, a permission or a switch.
 *
 * The time is the user's wait: from when they sent the message that opened the turn to when the live
 * stream brought this page the agent's last work in it, both on this page's clock. A turn this page did
 * not see sent, or did not see end (a reload, another page, one the agent opened itself), just says
 * "Done".
 */

import m from "mithril";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { getChatById } from "../models/Chats";
import { getOutgoingMessages, sentAtOfArrival } from "../models/OutgoingMessages";
import { liveArrivalOfAgentWork, type TranscriptEvent } from "../models/Response";
import { formatElapsed, latestTurnSpan } from "./turnTiming";

/** What the mark says for the transcript's latest turn, or null when it shows nothing. */
export function turnDoneLabel(chatId: string, events: readonly TranscriptEvent[]): string | null {
  const chat = getChatById(chatId);
  if (chat === undefined || (chat.status !== "idle" && chat.status !== "working") || chat.handoff !== null)
    return null;
  const agent = chat.active_agent;
  if (agent.activity_state !== "IDLE" || agent.queued_messages.length > 0 || agent.is_connecting) return null;
  if (getOutgoingMessages(chatId).length > 0) return null;
  const last = events.length === 0 ? undefined : events[events.length - 1];
  // A turn that ended on a provider failure shows that failure's notice, not a finish.
  if (last === undefined || (last.type === "assistant_message" && (last.is_api_error || last.is_auth_error))) {
    return null;
  }
  const span = latestTurnSpan(events);
  if (span === null || !span.hasAgentWork) return null;
  const sentAt = sentAtOfArrival(span.openingEventId);
  const doneAt = span.lastWorkEventId === null ? null : liveArrivalOfAgentWork(chatId, span.lastWorkEventId);
  if (sentAt === null || doneAt === null) return "Done";
  const took = doneAt - sentAt;
  return took < 1000 ? "Done" : `Done in ${formatElapsed(took)}`;
}

export function TurnDoneMark(): m.Component<{ chatId: string; events: TranscriptEvent[] }> {
  return {
    view(vnode) {
      const label = turnDoneLabel(vnode.attrs.chatId, vnode.attrs.events);
      if (label === null) return null;
      return m(
        "div",
        {
          class:
            "turn-done-mark mb-5 flex min-h-5 items-center gap-1.5 px-1 " +
            "text-(length:--font-size-helper) text-faint",
          role: "status",
        },
        [
          m(
            "span",
            { class: "turn-done-mark__check inline-flex shrink-0" },
            m.trust(icon("check", { size: 12, strokeWidth: 2.5 })),
          ),
          m("span", { class: "turn-done-mark__label" }, label),
        ],
      );
    },
  };
}
