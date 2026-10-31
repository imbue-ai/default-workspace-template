/**
 * "Last message 3 days ago", under the latest message of a chat that is not working: so the user can tell a chat
 * nobody has touched in days from one in use before stopping or deleting it. The exact time is its tooltip.
 *
 * The time is the transcript's own timestamp of the latest message from either side, so a helper chat messaged
 * only by the agent that started it reads as right as one the user talks to. A compaction, a system chip or
 * another injection the transcript records as a user message does not count. It hides while the agent works on a
 * turn (the status row says so) and while a send of this page is on its way.
 */

import m from "mithril";
import { getChatById } from "../models/Chats";
import { getOutgoingMessages } from "../models/OutgoingMessages";
import type { TranscriptEvent } from "../models/Response";
import { isTurnUserMessage } from "./message-classification";
import { Age, longAge } from "./relativeTime";

function isMessage(event: TranscriptEvent): boolean {
  if (event.type === "assistant_message") return true;
  return event.type === "user_message" && isTurnUserMessage(event);
}

/** When the transcript's latest message was, in epoch milliseconds, or null when the mark shows nothing. */
export function lastMessageAtMs(chatId: string, events: readonly TranscriptEvent[]): number | null {
  const chat = getChatById(chatId);
  if (chat === undefined || chat.status === "working" || chat.handoff !== null) return null;
  if (getOutgoingMessages(chatId).length > 0) return null;
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (!isMessage(event)) continue;
    const at = Date.parse(event.timestamp);
    if (!Number.isNaN(at)) return at;
  }
  return null;
}

export function LastMessageMark(): m.Component<{ chatId: string; events: TranscriptEvent[] }> {
  return {
    view(vnode) {
      const atMs = lastMessageAtMs(vnode.attrs.chatId, vnode.attrs.events);
      if (atMs === null) return null;
      return m("div", { class: "last-message-mark min-h-5 px-1 leading-5 text-faint" }, [
        "Last message ",
        m(Age, { atMs, format: longAge, tooltipLead: "Last message", class: "last-message-mark__age" }),
      ]);
    },
  };
}
