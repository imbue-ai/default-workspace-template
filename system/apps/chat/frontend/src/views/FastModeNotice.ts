/**
 * The one-time notice over the model bar: the chat app just turned fast mode off after the
 * workspace's turn limit, and the limit is in the model picker. Shown once per workspace (the
 * settings remember it), beside the bar whose fast switch it explains, until dismissed.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { getChatSettings } from "../models/ChatSettings";
import { dismissFastModeNotice, getFastModeNoticeChatId } from "./fast-mode-limit";

/** What the notice says for a limit of `turnLimit` turns. */
export function fastModeNoticeText(turnLimit: number): string {
  const turns = turnLimit === 1 ? "1 turn" : `${turnLimit} turns`;
  return `Fast mode is off now: new chats run fast for the first ${turns}. Change how many in the model picker.`;
}

export function FastModeNotice(): m.Component<{ chatId: string }> {
  return {
    view(vnode) {
      if (getFastModeNoticeChatId() !== vnode.attrs.chatId) return null;
      const turnLimit = getChatSettings()?.fast_mode_turn_limit ?? 0;
      return m(
        "div",
        {
          class:
            "fast-mode-notice absolute bottom-full left-0 mb-2 flex max-w-[360px] items-start gap-2 rounded-lg " +
            "border border-default bg-surface px-3 py-2 text-(length:--font-size-helper) text-secondary shadow-md",
          role: "status",
        },
        [
          m("span", { class: "mt-0.5 shrink-0 text-accent" }, m.trust(icon("zap", { size: 14, filled: true }))),
          m("span", { class: "min-w-0" }, fastModeNoticeText(turnLimit)),
          m(
            Button,
            {
              variant: "ghost",
              icon: true,
              xs: true,
              extra: "fast-mode-notice-dismiss shrink-0",
              "aria-label": "Dismiss",
              onclick: dismissFastModeNotice,
            },
            m.trust(icon("close", { size: 12, strokeWidth: 2.5 })),
          ),
        ],
      );
    },
  };
}
