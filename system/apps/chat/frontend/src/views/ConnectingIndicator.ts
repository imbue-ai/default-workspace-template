/**
 * Whether a sent message is waiting for the agent to come up, and what the status row at the end of
 * the transcript says about it: "Connecting to Opus 5.5 on Anthropic...", naming the model and the
 * provider as far as the chat knows them yet (the row itself is ActivityIndicator), or "Waking up..." when
 * the send found the agent stopped and has to start it again.
 *
 * Connecting is a sub-state of Sending (contract A1): the message already shows as sent, and
 * this says where the wait is. The backend reports it on the chat's snapshot
 * (``active_agent.is_connecting``). A chat still being created has no snapshot yet, and a message
 * sent to it waits for the create, so that wait reads the same way. A switching chat is left to
 * the handoff's own progress text.
 */

import { getChatById, getProvisionalChat } from "../models/Chats";
import type { ChatSnapshot } from "../models/Chats";
import { effectiveChoice } from "../models/ModelSettings";
import { getOutgoingMessages } from "../models/OutgoingMessages";
import { accountForAgent, accountForFirstSend } from "../models/Providers";
import { isAgentProcessDead } from "./agentLiveness";

// The chats whose current wait began with their agent stopped. The agent reads alive again before the wait is over
// (the chat app notes it as soon as the start returns), so the wait remembers how it began until it ends.
const wakingChatIds = new Set<string>();

/** Whether the listed chat's send is waiting on its stopped agent to start again, rather than on a first start.
 *  Read on every redraw of the status row and the composer, which is what keeps the latch in step. */
function isWakingUp(chat: ChatSnapshot): boolean {
  if (!chat.active_agent.is_connecting) {
    wakingChatIds.delete(chat.chat_id);
    return false;
  }
  if (isAgentProcessDead(chat.active_agent.state)) wakingChatIds.add(chat.chat_id);
  return wakingChatIds.has(chat.chat_id);
}

export function isChatConnecting(chatId: string): boolean {
  const chat = getChatById(chatId);
  if (chat !== undefined) {
    isWakingUp(chat);
    return chat.handoff === null && chat.active_agent.is_connecting;
  }
  const provisional = getProvisionalChat(chatId);
  return (
    provisional?.phase === "creating" && (provisional.held_sends.length > 0 || getOutgoingMessages(chatId).length > 0)
  );
}

/** What the status row says: the model and the provider the message is waiting for, as far as they are known. */
export function connectingLabel(chatId: string): string {
  const chat = getChatById(chatId);
  if (chat !== undefined && isWakingUp(chat)) return "Waking up…";
  const account =
    chat === undefined
      ? accountForFirstSend(getProvisionalChat(chatId)?.account_id)
      : accountForAgent(chat.active_agent.account_id ?? undefined);
  const model =
    chat === undefined ? null : (effectiveChoice(chatId, chat.active_agent.model_choice)?.matched?.label ?? null);
  const provider = account?.provider ?? null;
  if (model !== null && provider !== null) return `Connecting to ${model} on ${provider}…`;
  const target = model ?? provider;
  return target === null ? "Connecting…" : `Connecting to ${target}…`;
}
