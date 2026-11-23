/**
 * What a click on a checklist entry does: it shows the item's pointer, which whichever layer owns the thing
 * pointed at draws. The chat app draws the one at its New chat button, asked through the shell by a message whose
 * type the chat app registers; the Imbue Studio chrome draws the one in its share panel; the shell draws the two
 * cards, one popping out of the agent's avatar and one under the chrome's permissions button. The page names no
 * app.
 */

import type { ShellCoachCardPlace } from "@imbue/workspace-ui/src/app_contract";
import type { ShareSettingsCoachMark } from "@imbue/workspace-ui/src/embed";
import { BUILD_APP_COACH_TEXT, CONNECT_DATA_COACH_TEXT } from "@imbue/workspace-ui/src/onboarding_pointers";

/** The message, sent through the shell, that asks for the pointer at the chat list's New chat button. */
export const SHOW_NEW_CHAT_POINTER_MESSAGE = "onboarding:show-new-chat-pointer";

/** The ways out of the page a pointer is asked for through. */
export interface PointerChannels {
  /** A message for whichever app registered ``type`` (``shell:message``). */
  readonly sendMessage: (type: string, fields: Readonly<Record<string, unknown>>) => void;
  /** A one-button card the shell draws (``shell:show-coach-card``). */
  readonly showCoachCard: (text: string, place: ShellCoachCardPlace) => void;
  /** The chrome's share panel, opened with a bubble at one of its controls (``minds:open-share-settings``). */
  readonly openShareSettings: (coachMark: ShareSettingsCoachMark) => void;
}

/** Whether a click on the item's entry can show its pointer here: two of them need a chrome that draws coach
 *  marks, and an item this page does not know has none. */
export function isPointerAvailable(itemId: string, showsCoachMarks: boolean): boolean {
  switch (itemId) {
    case "new_chat":
    case "build_app":
      return true;
    case "phone_access":
    case "connect_data":
      return showsCoachMarks;
    default:
      return false;
  }
}

/** Show the item's pointer. */
export function showPointer(itemId: string, channels: PointerChannels): void {
  switch (itemId) {
    case "new_chat":
      channels.sendMessage(SHOW_NEW_CHAT_POINTER_MESSAGE, {});
      return;
    case "phone_access":
      channels.openShareSettings("mobile-access");
      return;
    case "build_app":
      channels.showCoachCard(BUILD_APP_COACH_TEXT, { target: "avatar" });
      return;
    case "connect_data":
      channels.showCoachCard(CONNECT_DATA_COACH_TEXT, { chromeAnchor: "permissions" });
      return;
    default:
      return;
  }
}
