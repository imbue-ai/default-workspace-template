/**
 * A chat's fast mode (the backend's ``ChatFastModeState``, at ``/api/chats/<id>/fast-mode``):
 * off, auto or on, and for auto whether the chat has run its fast turns and been switched to
 * standard speed. The choice belongs to the chat, so it lives on the backend beside the chat's
 * record.
 */

import { createChatSettingStore } from "./chatSettingStore";

export type FastModeMode = "off" | "auto" | "on";

export interface ChatFastModeState {
  mode: FastModeMode;
  // Auto only: the chat has run its fast turns and was switched to standard speed.
  is_switched: boolean;
}

export const {
  get: getFastModeState,
  ensure: ensureFastModeState,
  set: updateFastModeState,
  forgetAll: forgetAllFastModeStates,
} = createChatSettingStore<ChatFastModeState>({ endpoint: "fast-mode", description: "fast mode" });

/** What each mode is called, everywhere one is named. */
export const FAST_MODE_LABELS: Readonly<Record<FastModeMode, string>> = {
  off: "Off",
  auto: "Auto",
  on: "On",
};

/** The modes, in the order the chooser offers them, read off the table above. */
export const FAST_MODES: readonly FastModeMode[] = Object.keys(FAST_MODE_LABELS) as FastModeMode[];

/** What fast mode is, shown on hover over the model picker's fast row. */
export const FAST_MODE_EXPLANATION =
  "Fast mode makes the model reply faster. It's the same model, just as smart, only quicker. It costs more to run, so turn it on when speed matters and leave it off otherwise.";

/** What the model picker's fast row says for a state. */
export function fastModeLabel(state: ChatFastModeState): string {
  const label = FAST_MODE_LABELS[state.mode];
  return state.mode === "auto" && state.is_switched ? `${label} (off now)` : label;
}

/** The line under a mode in the chooser; auto's names the limit it runs to. */
export function fastModeDetail(mode: FastModeMode, turnLimit: number): string {
  switch (mode) {
    case "off":
      return "Standard speed always";
    case "on":
      return "Fast mode always";
    case "auto": {
      const turns = turnLimit === 1 ? "1 turn" : `${turnLimit} turns`;
      return `Fast for the first ${turns}, then standard`;
    }
  }
}
