/**
 * The pending lane (spec 5.1): the account the user picked in the provider row for a chat but
 * has not applied yet. Frontend-only state, like draft text: per chat, in memory, lost on
 * reload. It applies on the next send, which becomes a switch to that account, and it clears
 * on its own once the chat runs there.
 */

import { addChatsUpdatedListener, getChatById } from "./Chats";
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
 * The account the chat's next send switches it to, or null when the next send is an ordinary
 * one: nothing is pending, the pending account is gone, the chat already runs on it, or it runs
 * the chat's own harness (a rebind, which a later phase adds; the row never offers it).
 */
export function pendingSwitchTarget(chatId: string): ProviderAccount | null {
  const account = accountForAgent(getPendingAccountId(chatId) ?? undefined);
  const chat = getChatById(chatId);
  if (account === null || chat === undefined) return null;
  if (account.id === chat.active_agent.account_id || account.harness === chat.active_agent.harness) return null;
  return account;
}

/** Follow the chat list: a chat that now runs on its pending account has applied the choice. */
export function trackPendingLaneSettlement(): void {
  addChatsUpdatedListener((chats) => {
    for (const chat of chats) {
      if (pendingAccountIdByChat.get(chat.chat_id) === chat.active_agent.account_id) {
        pendingAccountIdByChat.delete(chat.chat_id);
      }
    }
  });
}
