/**
 * What a click on an action suggestion does (``models/NewChatSuggestions.ts``), and whether this page can do it.
 *
 * Each action points the user at a control, and the layer that owns the control draws the bubble: the chat root for
 * its New chat button, this page for its own composer, the shell for a card under the Imbue Studio chrome's
 * permissions button, and the chrome itself inside its share panel. A suggestion whose click needs a layer this
 * page cannot reach is not offered.
 */

import { showCoachMark } from "@imbue/workspace-ui/src/components/CoachMark";
import { framingChatRoot } from "../embedApi";
import type { SuggestionAction } from "../models/Suggestions";
import { openShareSettingsWithCoachMark, showCoachCard, showsCoachMarks } from "../shell";
import { focusComposer } from "./MessageInput";

export const BUILD_APP_COACH_TEXT =
  "Provide some context on a problem you're facing and tell the agent to build an app to help with it.";
export const CONNECT_DATA_COACH_TEXT =
  "Your connections live here. Ask me to connect any of these services, or any other data you want to pull in.";
// The composer's text area (``views/MessageInput.ts``), of which a page has one.
const COMPOSER_TEXTBOX_SELECTOR = ".message-input-textbox";

/** The chat root's coach mark at its New chat button, or null for a page no chat list frames, or one framed by a
 *  root loaded before it could draw the mark. */
function newChatCoachMarkOfRoot(): (() => void) | null {
  const root = framingChatRoot();
  if (root === null || typeof root.showNewChatCoachMark !== "function") return null;
  return () => root.showNewChatCoachMark();
}

/** Whether this page can do what ``action`` asks for. */
export function canRunSuggestionAction(action: SuggestionAction): boolean {
  switch (action) {
    case "new_chat_coach":
      return newChatCoachMarkOfRoot() !== null;
    case "phone_access":
    case "connect_data_coach":
      return showsCoachMarks();
    case "build_app_coach":
      return true;
    default: {
      const unknown: never = action;
      throw new Error(`Unknown suggestion action: ${String(unknown)}`);
    }
  }
}

/** Do what a click on a suggestion carrying ``action`` asks for. */
export function runSuggestionAction(action: SuggestionAction): void {
  switch (action) {
    case "new_chat_coach":
      newChatCoachMarkOfRoot()?.();
      return;
    case "phone_access":
      openShareSettingsWithCoachMark("mobile-access");
      return;
    case "build_app_coach": {
      const textbox = document.querySelector<HTMLElement>(COMPOSER_TEXTBOX_SELECTOR);
      if (textbox === null) return;
      showCoachMark({ anchor: textbox, text: BUILD_APP_COACH_TEXT });
      focusComposer();
      return;
    }
    case "connect_data_coach":
      showCoachCard(CONNECT_DATA_COACH_TEXT, "permissions");
      return;
    default: {
      const unknown: never = action;
      throw new Error(`Unknown suggestion action: ${String(unknown)}`);
    }
  }
}
