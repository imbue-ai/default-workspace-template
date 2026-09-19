/**
 * Fast mode's three settings and the automatic switch behind ``auto``.
 *
 * A chat runs in one of three modes (models/FastMode.ts): ``off``, ``auto`` (fast for the
 * workspace's configured number of the user's turns, then standard speed) and ``on``. This module
 * decides when auto's "then" is, and applies a mode the user picks (in the model picker's Fast
 * Mode submenu, or with ``/fast on`` and ``/fast off`` in the composer).
 *
 * The check runs per render, where the loaded transcript and the idle flag meet, on a chat whose
 * harness declared the ``fast_mode_limit`` turn check (claude, codex). Auto switches a chat at
 * most once, recorded on the chat's state, so a user who turns fast mode back on afterwards keeps
 * it. The first switch in a workspace also raises a short notice explaining what happened and
 * where the mode lives, recorded on the settings so it shows once.
 *
 * A turn is counted exactly as the transcript view counts one, by reusing the boundary rule the
 * timeline groups on, so "5 turns" means five exchanges the user can see. Permission verdicts
 * are excluded on top of that (the app talking to itself), and so are the turns of a seeded
 * chat's seed segment: the Mind app wrote those before any agent ran, so they bought no fast
 * turns.
 */

import m from "mithril";
import type { ChatSnapshot } from "../models/Chats";
import {
  DEFAULT_CHAT_SETTINGS,
  ensureChatSettings,
  getChatSettings,
  updateChatSettings,
} from "../models/ChatSettings";
import type { FastModeMode } from "../models/FastMode";
import { ensureFastModeState, getFastModeState, updateFastModeState } from "../models/FastMode";
import { hasFastModeLimit } from "../models/HarnessCatalog";
import { getChatFastMode, setFastMode } from "../models/ModelSettings";
import type { TranscriptEvent } from "../models/Response";
import { SEED_SOURCE } from "../models/Response";
import { isNonBoundaryUserMessage, resolutionOf } from "./message-classification";

// The chat whose switch raised the notice, or null while none is showing.
let noticeChatId: string | null = null;

/** How many turns the user has actually taken with an agent in this conversation. */
export function countUserTurns(events: readonly TranscriptEvent[]): number {
  let count = 0;
  for (const event of events) {
    if (event.type !== "user_message") {
      continue;
    }
    if (event.source === SEED_SOURCE) {
      continue;
    }
    if (isNonBoundaryUserMessage(event)) {
      continue;
    }
    if (resolutionOf(event) !== null) {
      continue;
    }
    count = count + 1;
  }
  return count;
}

/**
 * Whether this conversation has run its fast turns and should be switched to standard speed now.
 *
 * Requires the agent to be idle so the switch lands between turns rather than interrupting a
 * reply, and fast mode to still be on (a user who turned it off has nothing to switch).
 */
export function isFastModeLimitReached(
  chat: ChatSnapshot | undefined,
  events: readonly TranscriptEvent[],
  isAgentIdle: boolean,
  fastModeTurnLimit: number,
): boolean {
  if (chat === undefined || !hasFastModeLimit(chat.active_agent.harness)) {
    return false;
  }
  if (fastModeTurnLimit <= 0 || !isAgentIdle || !getChatFastMode(chat.chat_id)) {
    return false;
  }
  return countUserTurns(events) >= fastModeTurnLimit;
}

/** The chat whose switch raised the one-time notice, or null. */
export function getFastModeNoticeChatId(): string | null {
  return noticeChatId;
}

export function dismissFastModeNotice(): void {
  noticeChatId = null;
  m.redraw();
}

/**
 * Switch an auto-mode chat to standard speed if it has run its fast turns. Safe to call on every
 * render: the cheap gates run first, the settings and the chat's mode load once, and a chat is
 * switched once.
 */
export function maybeApplyFastModeLimit(
  chat: ChatSnapshot | undefined,
  events: readonly TranscriptEvent[],
  isAgentIdle: boolean,
): void {
  if (chat === undefined || !hasFastModeLimit(chat.active_agent.harness)) {
    return;
  }
  const settings = getChatSettings();
  if (settings === null) {
    void ensureChatSettings();
    return;
  }
  const state = getFastModeState(chat.chat_id);
  if (state === null) {
    void ensureFastModeState(chat.chat_id);
    return;
  }
  if (state.mode !== "auto" || state.is_switched) {
    return;
  }
  if (!isFastModeLimitReached(chat, events, isAgentIdle, settings.fast_mode_turn_limit)) {
    return;
  }
  void updateFastModeState(chat.chat_id, { mode: "auto", is_switched: true });
  setFastMode(chat.chat_id, false);
  if (!settings.is_fast_mode_notice_shown) {
    noticeChatId = chat.chat_id;
    void updateChatSettings({ ...settings, is_fast_mode_notice_shown: true });
  }
}

/**
 * Put the chat in a mode the user chose: the mode is recorded on the chat, and the agent's speed
 * follows it at once (auto counts the turns already taken, so a long chat put on auto is already
 * past its fast turns).
 */
export function chooseFastMode(chatId: string, mode: FastModeMode, events: readonly TranscriptEvent[]): void {
  const limit = getChatSettings()?.fast_mode_turn_limit ?? DEFAULT_CHAT_SETTINGS.fast_mode_turn_limit;
  const isSwitched = mode === "auto" && countUserTurns(events) >= limit;
  void updateFastModeState(chatId, { mode, is_switched: isSwitched });
  setFastMode(chatId, mode === "on" || (mode === "auto" && !isSwitched));
}

/** The mode a composer command names: ``/fast on`` or ``/fast off``; null for any other text. */
export function parseFastModeCommand(text: string): FastModeMode | null {
  const tokens = text.trim().toLowerCase().split(/\s+/);
  if (tokens.length !== 2 || tokens[0] !== "/fast") {
    return null;
  }
  if (tokens[1] === "on" || tokens[1] === "off") {
    return tokens[1];
  }
  return null;
}

/** Forget the notice, so a test starts clean. */
export function resetFastModeLimitForTests(): void {
  noticeChatId = null;
}
