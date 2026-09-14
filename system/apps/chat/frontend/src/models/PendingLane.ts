/**
 * The pending lane (spec 5.1): the account the user picked in the provider row for a chat but
 * has not applied yet. Frontend-only state, like draft text: per chat, in memory, lost on
 * reload. It applies on the next send, which becomes a switch to that account, and it clears
 * on its own once the chat runs there.
 */

import { addActiveAgentChangedListener, addChatsUpdatedListener, getChatById } from "./Chats";
import type { ChatSnapshot, TransitionKind } from "./Chats";
import { accountForAgent } from "./Providers";
import type { ProviderAccount } from "./Providers";

const pendingAccountIdByChat = new Map<string, string>();

/** Choose the account the chat's next send switches it to, or clear the choice with null. */
export function setPendingAccount(chatId: string, accountId: string | null): void {
  if (accountId === null) {
    pendingAccountIdByChat.delete(chatId);
  } else {
    pendingAccountIdByChat.set(chatId, accountId);
  }
}

export function getPendingAccountId(chatId: string): string | null {
  return pendingAccountIdByChat.get(chatId) ?? null;
}

/**
 * Whether a send to ``account`` moves the chat: any signed-in account but the one the chat runs
 * on. The backend decides what the move is (spec 5.2): a rebind of the same agent for an account
 * on the chat's own harness and lane, a handoff to a new agent otherwise; ``switchKind`` is the
 * page's reading of the same rule, for the words the confirm uses.
 */
export function isSwitchTarget(chat: ChatSnapshot, account: ProviderAccount): boolean {
  return account.id !== chat.active_agent.account_id;
}

/** What switching the chat to ``account`` does: keep the agent and change its account, or replace the agent. */
export function switchKind(chat: ChatSnapshot, account: ProviderAccount): TransitionKind {
  const own = accountForAgent(chat.active_agent.account_id ?? undefined);
  const isSameLane = own !== null && own.lane === account.lane;
  return account.harness === chat.active_agent.harness && isSameLane ? "rebind" : "handoff";
}

/**
 * The account the chat's next send switches it to, or null when the next send is an ordinary
 * one: nothing is pending, the pending account is gone, or it is not a switch target.
 */
export function pendingSwitchTarget(chatId: string): ProviderAccount | null {
  const account = accountForAgent(getPendingAccountId(chatId) ?? undefined);
  const chat = getChatById(chatId);
  if (account === null || chat === undefined || !isSwitchTarget(chat, account)) return null;
  return account;
}

/**
 * Follow the chat list: a chat that now runs on its pending account has applied the choice, and
 * so has one that moved to a new agent, whatever account that agent runs on (a failed switch
 * retried from its notice on another account lands there, not on the one picked). A cancelled
 * switch changes neither, so the lane survives it for the next try (spec 5.6).
 */
export function trackPendingLaneSettlement(): void {
  addChatsUpdatedListener((chats) => {
    for (const chat of chats) {
      if (pendingAccountIdByChat.get(chat.chat_id) === chat.active_agent.account_id) {
        pendingAccountIdByChat.delete(chat.chat_id);
      }
    }
  });
  addActiveAgentChangedListener((chatId) => {
    pendingAccountIdByChat.delete(chatId);
  });
}
