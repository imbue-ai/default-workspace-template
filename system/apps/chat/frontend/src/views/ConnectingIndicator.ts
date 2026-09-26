/**
 * "Connecting..." beside the model bar: a sent message is waiting for the agent to come up.
 *
 * Connecting is a sub-state of Sending (contract A1): the message already shows as sent, and
 * this says where the wait is. The backend reports it on the chat's snapshot
 * (``active_agent.is_connecting``). A chat still being created has no snapshot yet, and a message
 * sent to it waits for the create, so that wait reads the same way. A switching chat is left to
 * the handoff's own progress text.
 */

import m from "mithril";
import { activityDotClass } from "@imbue/workspace-ui/src/components/activityDot";
import { getChatById, getProvisionalChat } from "../models/Chats";
import { getOutgoingMessages } from "../models/OutgoingMessages";

export function isChatConnecting(chatId: string): boolean {
  const chat = getChatById(chatId);
  if (chat !== undefined) {
    return chat.handoff === null && chat.active_agent.is_connecting;
  }
  return getProvisionalChat(chatId)?.phase === "creating" && getOutgoingMessages(chatId).length > 0;
}

export function ConnectingIndicator(): m.Component<{ chatId: string }> {
  return {
    view(vnode) {
      if (!isChatConnecting(vnode.attrs.chatId)) return null;
      return m(
        "div",
        {
          class: "connecting-indicator flex items-center gap-1.5 px-1 type-helper whitespace-nowrap text-secondary",
          role: "status",
          "aria-live": "polite",
        },
        [
          m("span", { class: `connecting-indicator__dot ${activityDotClass("h-1.5 w-1.5", "warning")}` }),
          m("span", { class: "connecting-indicator__label" }, "Connecting…"),
        ],
      );
    },
  };
}
