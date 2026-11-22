/**
 * What the two placements of suggestions share above the composer (``views/ChatSuggestions.ts``): the shape of a
 * strip a placement hands over to be drawn, and the link of a strip that switches its placement off.
 *
 * The link switches the placement off for the whole workspace: the strip goes, a line saying so takes its place for
 * a few seconds with a way back, and an Imbue Studio chrome that opens its workspace settings is asked to.
 */

import m from "mithril";
import { isSuggestionSwitchOn, updateSuggestionSwitches } from "../models/ChatSettings";
import { SUGGESTION_SWITCHES } from "../models/Suggestions";
import type { Suggestion, SuggestionPlacement } from "../models/Suggestions";
import { openWorkspaceSettings, opensWorkspaceSettings } from "../shell";

/** A placement's strip, as the placement hands it over to be drawn. */
export interface PlacementStrip {
  placement: SuggestionPlacement;
  // Names the set the strip holds, so another set's strip rises in rather than changing in place.
  setKey: string;
  suggestions: readonly Suggestion[];
  /** Its pills are shown: the next-step strip's are not waiting behind its line, and a new-chat strip's always
   *  are. */
  isOpen: boolean;
  onPick: (suggestion: Suggestion) => void;
  /** Shows the pills from the strip's line, or hides them behind it; null for a strip whose pills are always
   *  shown. */
  onToggle: (() => void) | null;
}

// The group of the chrome's workspace settings that holds the suggestion switches.
const SUGGESTIONS_SETTINGS_GROUP = "suggestions";
export const SUGGESTIONS_OFF_NOTICE_MS = 8_000;

/** The link that switches one placement off, and the line with Undo that follows a click on it. */
export interface SuggestionSwitchLink {
  disable: () => void;
  undo: () => void;
  /** Whether the line saying the placement is off is up. ``isEnabled`` true takes it down: the placement was
   *  switched back on from somewhere else, or the write was refused, and the line would be wrong. */
  isOffNoticeShown: (isEnabled: boolean) => boolean;
  /** Drop the line's timer, as the strips' view goes. */
  dispose: () => void;
}

export function makeSuggestionSwitchLink(placement: SuggestionPlacement): SuggestionSwitchLink {
  const switchName = SUGGESTION_SWITCHES[placement];
  let isOffNoticeShown = false;
  let offNoticeTimer: ReturnType<typeof setTimeout> | null = null;
  // How many times the link or Undo has changed the switch, so a write can tell whether it is still the latest.
  let switchChangeCount = 0;

  function hideOffNotice(): void {
    isOffNoticeShown = false;
    if (offNoticeTimer !== null) clearTimeout(offNoticeTimer);
    offNoticeTimer = null;
  }

  return {
    disable() {
      const change = ++switchChangeCount;
      // The chrome's settings read the switches as they open, so they are opened only once the write has answered,
      // and not at all when it was refused or Undo was clicked while it was out.
      void updateSuggestionSwitches({ [switchName]: false }).then(() => {
        if (change !== switchChangeCount) return;
        if (isSuggestionSwitchOn(switchName) === false && opensWorkspaceSettings()) {
          openWorkspaceSettings(SUGGESTIONS_SETTINGS_GROUP);
        }
      });
      hideOffNotice();
      isOffNoticeShown = true;
      offNoticeTimer = setTimeout(() => {
        hideOffNotice();
        m.redraw();
      }, SUGGESTIONS_OFF_NOTICE_MS);
    },
    undo() {
      switchChangeCount++;
      hideOffNotice();
      void updateSuggestionSwitches({ [switchName]: true });
    },
    isOffNoticeShown(isEnabled) {
      if (isOffNoticeShown && isEnabled) hideOffNotice();
      return isOffNoticeShown;
    },
    dispose: hideOffNotice,
  };
}
