/**
 * The send-button hint: the first time anyone in a workspace picks a category from a first-run picker
 * (``views/StudioPickers.ts``), a coach mark at the composer's send button says what to press next. A pick only
 * edits the draft, and somebody who has never sent a message here may not know the arrow sends it.
 *
 * Shown once per workspace: the chat settings record it, as they record the other one-time notices. It goes as any
 * coach mark does, and when a message is sent by any other way than the button (the Enter key, a suggestion).
 */

import { showCoachMark } from "@imbue/workspace-ui/src/components/CoachMark";
import { ensureChatSettings, getChatSettings, updateChatSettings } from "../models/ChatSettings";
import { addMessageSentListener, removeMessageSentListener } from "../models/Response";

export const PICKER_SEND_HINT_TEXT = "Click this button to send your message";
// The composer's send button (``views/MessageInput.ts``), of which a page has one.
const SEND_BUTTON_SELECTOR = ".message-input-send-button";

let isHintBeingDecided = false;

/** Show the hint unless this workspace has seen it; called on a pick that added a category to the draft. */
export function showPickerSendHintOnce(): void {
  if (isHintBeingDecided || getChatSettings()?.is_picker_send_hint_shown === true) return;
  isHintBeingDecided = true;
  void ensureChatSettings()
    .then(() => {
      // Read after the load rather than from its result: a load that failed resolves with defaults that are no
      // record of what this workspace has seen, and showing the hint on them would show it on every pick.
      const settings = getChatSettings();
      const sendButton = document.querySelector<HTMLElement>(SEND_BUTTON_SELECTOR);
      if (settings === null || settings.is_picker_send_hint_shown === true || sendButton === null) return;
      const dismissOnSend = (): void => hint.dismiss();
      const hint = showCoachMark({
        anchor: sendButton,
        text: PICKER_SEND_HINT_TEXT,
        onDismiss: () => removeMessageSentListener(dismissOnSend),
      });
      addMessageSentListener(dismissOnSend);
      void updateChatSettings({ ...settings, is_picker_send_hint_shown: true });
    })
    .finally(() => {
      isHintBeingDecided = false;
    });
}
