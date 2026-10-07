/**
 * The one-time notice that idle chats now compact on their own. Shown once per workspace (the
 * settings remember it) until dismissed.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { ensureChatSettings, getChatSettings, updateChatSettings } from "../models/ChatSettings";

export const AUTOCOMPACT_NOTICE_TEXT =
  "Idle chats now compact automatically to keep replies fast and cheap. Turn this off per chat, or " +
  "for new chats, under Auto-compact in the model menu.";

function dismissAutocompactNotice(): void {
  const settings = getChatSettings();
  if (settings === null) return;
  void updateChatSettings({ ...settings, is_autocompact_notice_shown: true });
}

export function AutocompactNotice(): m.Component {
  return {
    view() {
      // Nothing before the settings load: a workspace that has already dismissed it must not see
      // it flash in for the defaults.
      const settings = getChatSettings();
      if (settings === null) {
        void ensureChatSettings();
        return null;
      }
      if (settings.is_autocompact_notice_shown) return null;
      return m(
        "div",
        {
          class:
            "autocompact-notice flex max-w-[360px] items-start gap-2 rounded-lg border border-default bg-surface " +
            "px-3 py-2 text-(length:--font-size-helper) text-secondary shadow-md",
          role: "status",
        },
        [
          m("span", { class: "mt-0.5 shrink-0 text-accent" }, m.trust(icon("sparkle", { size: 14 }))),
          m("span", { class: "min-w-0" }, AUTOCOMPACT_NOTICE_TEXT),
          m(
            Button,
            {
              variant: "ghost",
              icon: true,
              xs: true,
              extra: "autocompact-notice-dismiss shrink-0",
              "aria-label": "Dismiss",
              onclick: dismissAutocompactNotice,
            },
            m.trust(icon("close", { size: 12, strokeWidth: 2.5 })),
          ),
        ],
      );
    },
  };
}
