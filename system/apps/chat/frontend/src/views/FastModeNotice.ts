/**
 * The one-time notice over the model bar: auto mode just switched the chat to standard speed
 * after the workspace's turn limit, and the mode is in the model picker. Shown once per workspace
 * (the settings remember it), beside the bar whose fast row it explains, until dismissed.
 */

import m from "mithril";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { getChatSettings } from "../models/ChatSettings";
import { getChatById } from "../models/Chats";
import { getFastModeLimitNoticeBody } from "../models/HarnessCatalog";
import { renderDismissibleNotice } from "./dismissible-notice";
import { dismissFastModeNotice, getFastModeNoticeChatId } from "./fast-mode-limit";

/** What the notice says for a limit of `turnLimit` turns, followed by `noticeBody` when the
 *  chat's harness declared one on its turn-limit popup. */
export function fastModeNoticeText(turnLimit: number, noticeBody: string | null): string {
  const turns = turnLimit === 1 ? "1 turn" : `${turnLimit} turns`;
  const body = `Fast mode is off now: this chat ran fast for its first ${turns}. Change this in the model picker.`;
  return noticeBody === null ? body : `${body} ${noticeBody}`;
}

export function FastModeNotice(): m.Component<{ chatId: string }> {
  return {
    view(vnode) {
      if (getFastModeNoticeChatId() !== vnode.attrs.chatId) return null;
      const turnLimit = getChatSettings()?.fast_mode_turn_limit ?? 0;
      const noticeBody = getFastModeLimitNoticeBody(getChatById(vnode.attrs.chatId)?.active_agent.harness);
      return renderDismissibleNotice({
        extraClass: "fast-mode-notice absolute bottom-full left-0 mb-2",
        iconHtml: icon("zap", { size: 14, filled: true }),
        text: fastModeNoticeText(turnLimit, noticeBody),
        dismissExtra: "fast-mode-notice-dismiss",
        onDismiss: dismissFastModeNotice,
      });
    },
  };
}
