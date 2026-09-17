/**
 * The pending switch (spec 5.1): the account the user chose in the switch dialog for a chat but
 * has not applied yet, with the model they picked for it. Frontend-only state, like draft text:
 * per chat, in memory, lost on reload. It applies on the next send, which becomes a switch to
 * that account, and it clears on its own once the chat runs there.
 */

import { addActiveAgentChangedListener, addChatsUpdatedListener, getChatById } from "./Chats";
import type { ChatSnapshot, TransitionKind } from "./Chats";
import type { ModelIdentity } from "./ModelSettings";
import { accountForAgent } from "./Providers";
import type { ProviderAccount } from "./Providers";

/** The model the successor of a handoff runs on, as picked in the switch dialog: the identity the
 *  switch request carries, and the label the strip and the model bar show for it. */
export interface PendingPick {
  identity: ModelIdentity;
  label: string;
}

const pendingAccountIdByChat = new Map<string, string>();
const pendingPickByChat = new Map<string, PendingPick>();

/** Choose the account the chat's next send switches it to, or clear the choice with null. A pick
 *  made for an earlier choice does not survive: it named a model of that account's harness. */
export function setPendingAccount(chatId: string, accountId: string | null): void {
  setPendingSwitch(chatId, accountId, null);
}

/** Choose the account and the model the chat's next send switches it to. */
export function setPendingSwitch(chatId: string, accountId: string | null, pick: PendingPick | null): void {
  pendingPickByChat.delete(chatId);
  if (accountId === null) {
    pendingAccountIdByChat.delete(chatId);
    return;
  }
  pendingAccountIdByChat.set(chatId, accountId);
  if (pick !== null) pendingPickByChat.set(chatId, pick);
}

export function getPendingAccountId(chatId: string): string | null {
  return pendingAccountIdByChat.get(chatId) ?? null;
}

/** The model picked for the pending switch, or null for the target harness's default (and for a rebind). */
export function getPendingPick(chatId: string): PendingPick | null {
  return pendingPickByChat.get(chatId) ?? null;
}

/**
 * Whether a send to ``account`` moves the chat: any signed-in account but the one the chat runs
 * on. The backend decides what the move is (spec 5.2): a rebind of the same agent for an account
 * on the chat's own harness and lane, a handoff to a new agent otherwise; ``switchKind`` is the
 * page's reading of the same rule, for the words the dialog uses.
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
 * switch changes neither, so the choice survives it for the next try (spec 5.6).
 */
export function trackPendingLaneSettlement(): void {
  addChatsUpdatedListener((chats) => {
    for (const chat of chats) {
      if (pendingAccountIdByChat.get(chat.chat_id) === chat.active_agent.account_id) {
        setPendingSwitch(chat.chat_id, null, null);
      }
    }
  });
  addActiveAgentChangedListener((chatId) => {
    setPendingSwitch(chatId, null, null);
  });
}
