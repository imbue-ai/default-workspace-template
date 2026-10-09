/**
 * The one-time notice that idle chats now compact on their own. Shown once per workspace (the
 * settings remember it) until dismissed.
 */

import m from "mithril";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { ensureChatSettings, getChatSettings, updateChatSettings } from "../models/ChatSettings";
import { renderDismissibleNotice } from "./dismissible-notice";

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
      return renderDismissibleNotice({
        extraClass: "autocompact-notice",
        iconHtml: icon("sparkle", { size: 14 }),
        text: AUTOCOMPACT_NOTICE_TEXT,
        dismissExtra: "autocompact-notice-dismiss",
        onDismiss: dismissAutocompactNotice,
      });
    },
  };
}
