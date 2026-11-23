/**
 * What the shell does for the tour (``TourAction``), over the store. Each names no app: the avatar's window is the
 * pinned one whose pin declares the avatar style, and the onboarding checklist is whichever app registered the
 * message's type.
 */

import { ONBOARDING_CHECKLIST_FRAME, ONBOARDING_CHECKLIST_MESSAGE_TYPE } from "../model/tourCards";
import type { TourAction } from "../model/tourCards";
import { avatarPinnedWindow } from "../reducers/desktopState";
import type { DesktopStore } from "./DesktopStore";

export function runTourAction(store: DesktopStore, action: TourAction): void {
  switch (action) {
    case "snap-avatar-window-right": {
      const window = avatarPinnedWindow(store.getState());
      if (window !== null) store.setWindowState(window.id, "SNAPPED_RIGHT");
      return;
    }
    case "show-avatar-window": {
      const window = avatarPinnedWindow(store.getState());
      if (window !== null) store.showWindow(window.id);
      return;
    }
    case "show-onboarding-checklist":
      void store.showRegisteredPageAt(ONBOARDING_CHECKLIST_MESSAGE_TYPE, ONBOARDING_CHECKLIST_FRAME);
      return;
  }
}
