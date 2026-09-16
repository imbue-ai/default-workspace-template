/**
 * The fast-mode turn limit: a new chat runs fast for the workspace's configured number of the
 * user's turns, then the chat app turns fast mode off. This module decides when "then" is.
 *
 * The check runs per render, where the loaded transcript and the idle flag meet, on a chat
 * whose harness declared the ``fast_mode_limit`` turn check (claude, codex). A chat is switched
 * at most once: a user who turns fast mode back on afterwards keeps it. The first switch in a
 * workspace also raises a short notice explaining what happened and where the limit lives,
 * recorded on the settings so it shows once.
 *
 * A turn is counted exactly as the transcript view counts one, by reusing the boundary rule the
 * timeline groups on, so "5 turns" means five exchanges the user can see. Permission verdicts
 * are excluded on top of that (the app talking to itself), and so are the turns of a seeded
 * chat's seed segment: the Mind app wrote those before any agent ran, so they bought no fast
 * turns.
 */

import m from "mithril";
import type { ChatSnapshot } from "../models/Chats";
import { ensureChatSettings, getChatSettings, updateChatSettings } from "../models/ChatSettings";
import { hasFastModeLimit } from "../models/HarnessCatalog";
import { getChatFastMode, setFastMode } from "../models/ModelSettings";
import type { TranscriptEvent } from "../models/Response";
import { SEED_SOURCE } from "../models/Response";
import { isNonBoundaryUserMessage, resolutionOf } from "./message-classification";

// The chats this page has already switched to standard speed; never switched twice.
const switchedChatIds = new Set<string>();
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
 * reply, fast mode to still be on (a user who turned it off has nothing to switch), and a limit
 * above zero (zero launches chats at standard speed, so there is nothing to time).
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

/** Whether this page already switched the chat off fast mode. */
export function wasFastModeLimitApplied(chatId: string): boolean {
  return switchedChatIds.has(chatId);
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
 * Switch the chat to standard speed if it has run its fast turns. Safe to call on every render:
 * the cheap gates run first, the settings load once, and a chat is switched once.
 */
export function maybeApplyFastModeLimit(
  chat: ChatSnapshot | undefined,
  events: readonly TranscriptEvent[],
  isAgentIdle: boolean,
): void {
  if (chat === undefined || switchedChatIds.has(chat.chat_id) || !hasFastModeLimit(chat.active_agent.harness)) {
    return;
  }
  const settings = getChatSettings();
  if (settings === null) {
    void ensureChatSettings().then(() => m.redraw());
    return;
  }
  if (!isFastModeLimitReached(chat, events, isAgentIdle, settings.fast_mode_turn_limit)) {
    return;
  }
  switchedChatIds.add(chat.chat_id);
  setFastMode(chat.chat_id, false);
  if (!settings.is_fast_mode_notice_shown) {
    noticeChatId = chat.chat_id;
    void updateChatSettings({ ...settings, is_fast_mode_notice_shown: true });
  }
}

/** Forget every switch and the notice, so a test starts clean. */
export function resetFastModeLimitForTests(): void {
  switchedChatIds.clear();
  noticeChatId = null;
}
