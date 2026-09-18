/**
 * The pending switch (spec 5.1): the account the user chose in the switch dialog for a chat but
 * has not applied yet, with the model they picked for it. Frontend-only state, like draft text:
 * per chat, in memory, lost on reload. It applies on the next send, which becomes a switch to
 * that account, and it clears on its own once the chat runs there.
 */

import { addChatsUpdatedListener, getChatById } from "./Chats";
import type { ChatSnapshot, TransitionKind } from "./Chats";
import type { CatalogModelOption } from "./HarnessCatalog";
import { showSwitchChoice } from "./ModelSettings";
import type { ModelIdentity } from "./ModelSettings";
import { accountForAgent } from "./Providers";
import type { ProviderAccount } from "./Providers";

/** The model the chat runs on after the switch, as picked in the switch dialog: the identity the
 *  switch request carries, and the label the strip and the model bar show for it. */
export interface PendingPick {
  identity: ModelIdentity;
  label: string;
  // The catalog option the label was built from, handed to the model bar's overlay once the switch
  // stops covering the pick, so the chip keeps reading it until the harness confirms it.
  option: CatalogModelOption;
}

const pendingAccountIdByChat = new Map<string, string>();
const pendingPickByChat = new Map<string, PendingPick>();
// The agent each chat was running on when its choice was made: a chat that has moved off it has
// spent the choice, wherever it landed.
const armedAgentIdByChat = new Map<string, string>();

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
    armedAgentIdByChat.delete(chatId);
    return;
  }
  pendingAccountIdByChat.set(chatId, accountId);
  const armedOn = getChatById(chatId)?.active_agent.agent_id;
  if (armedOn === undefined) armedAgentIdByChat.delete(chatId);
  else armedAgentIdByChat.set(chatId, armedOn);
  if (pick !== null) pendingPickByChat.set(chatId, pick);
}

export function getPendingAccountId(chatId: string): string | null {
  return pendingAccountIdByChat.get(chatId) ?? null;
}

/** The model picked for the pending switch, or null for none: a handoff's successor then starts on its
 *  harness's default, and a rebind keeps the agent's own model. */
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

/** Whether a switch is still being carried out, so the choice it is carrying is not spent yet. A
 *  failed one is done: its notice governs from there, and its retry names its own account. */
function isSwitchUnderway(chat: ChatSnapshot): boolean {
  return chat.handoff !== null && chat.handoff.phase !== "failed";
}

/** Whether ``chat`` has applied the choice it was carrying: it runs on the account that was chosen,
 *  or it has moved off the agent the choice was made on, whatever account it landed on (a failed
 *  switch retried from its notice on another account lands there, not on the one picked). */
function hasSpentItsChoice(chat: ChatSnapshot): boolean {
  if (pendingAccountIdByChat.get(chat.chat_id) === chat.active_agent.account_id) return true;
  const armedOn = armedAgentIdByChat.get(chat.chat_id);
  return armedOn !== undefined && armedOn !== chat.active_agent.agent_id;
}

/** Give up the choice ``chat`` was carrying, leaving the model bar showing the model it picked until
 *  the harness reports it: the chip would otherwise fall back to the pushed live choice, which does
 *  not carry the pick until the harness has written it.
 *
 *  A switch that failed carries nothing over: it never applied the pick, whether it broke at the
 *  agent's start or at the pick itself, so the chip stays on the model the agent is really on and
 *  the failure notice says what happened. */
function settle(chat: ChatSnapshot): void {
  const pick = pendingPickByChat.get(chat.chat_id);
  setPendingSwitch(chat.chat_id, null, null);
  if (pick !== undefined && chat.handoff === null) showSwitchChoice(chat.chat_id, pick.identity, pick.option);
}

/**
 * Follow the chat list: a chat that has applied its choice gives it up. A cancelled switch applies
 * nothing, so the choice survives it for the next try (spec 5.6).
 *
 * Never while the switch is still running. A rebind relabels the agent with the target account
 * partway through its restart -- before the agent is even started, let alone put on the picked
 * model -- and a handoff's successor becomes the chat's agent before the harness has reported the
 * model applied to it; either reading, taken then, would give the choice up in the middle of the
 * switch, which is exactly where the pushed live choice cannot be trusted.
 */
export function trackPendingLaneSettlement(): void {
  addChatsUpdatedListener((chats) => {
    for (const chat of chats) {
      if (!isSwitchUnderway(chat) && hasSpentItsChoice(chat)) settle(chat);
    }
  });
}
