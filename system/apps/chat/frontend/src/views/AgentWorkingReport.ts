/**
 * What a chat page tells the shell of the chat it shows: whether a reply is under way in it
 * (``shell.ts``'s ``reportAgentWorking``, which passes on only a change). The shell's show-around tour begins when
 * an agent goes to work, and a chat page knows a turn's start and end on every harness.
 *
 * The component draws nothing. It reads the chat on every draw of the page, whatever else the page draws, and
 * takes its word back when the page stops showing the chat.
 */

import type m from "mithril";
import { isRoomChat } from "../models/Chats";
import { reportAgentWorking } from "../shell";
import { isTurnDoneForWorkingReport } from "./last-reply";

// Whether each chat's turn was done at the last look that could tell.
const wasTurnDoneByChat = new Map<string, boolean>();

/** Whether a reply is under way in the chat, as far as the page has been able to tell: what it last knew while it
 *  cannot tell now, and no before it ever could. Never in a room, whose reader works on no reply. */
export function isReplyUnderWay(chatId: string): boolean {
  if (isRoomChat(chatId)) return false;
  const isTurnDone = isTurnDoneForWorkingReport(chatId);
  if (isTurnDone !== null) wasTurnDoneByChat.set(chatId, isTurnDone);
  return wasTurnDoneByChat.get(chatId) === false;
}

export function AgentWorkingReport(): m.Component<{ chatId: string }> {
  return {
    view({ attrs }) {
      reportAgentWorking(isReplyUnderWay(attrs.chatId));
      return null;
    },

    onremove() {
      reportAgentWorking(false);
    },
  };
}

/** Forget what was read of every chat's turn (tests). */
export function resetAgentWorkingReportForTests(): void {
  wasTurnDoneByChat.clear();
}
