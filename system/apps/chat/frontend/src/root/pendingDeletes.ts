/**
 * The chats the chat root has taken off its list while their delete is on its way: the list, the phone header and the
 * send picker lose the chat the moment the delete is confirmed, while the chat app runs it (an mngr destroy, which
 * takes seconds). The chat stays off until a chat list no longer carries it; a refused delete is dropped here, so the
 * chat comes back with the refusal.
 */

import type { ChatRow } from "./rows";

const pendingChatIds = new Set<string>();

/** Take ``chatId`` off the list while its delete is on its way. */
export function showPendingDelete(chatId: string): void {
  pendingChatIds.add(chatId);
}

/** The chat app refused the delete of ``chatId``: the chat stands. */
export function dropPendingDelete(chatId: string): void {
  pendingChatIds.delete(chatId);
}

/** A chat list arrived: a chat it no longer carries is gone, so there is nothing left to hide. Answers those chats. */
export function settlePendingDeletes(listedChatIds: ReadonlySet<string>): string[] {
  const goneChatIds = [...pendingChatIds].filter((chatId) => !listedChatIds.has(chatId));
  for (const chatId of goneChatIds) pendingChatIds.delete(chatId);
  return goneChatIds;
}

export function hasPendingDelete(): boolean {
  return pendingChatIds.size > 0;
}

/** ``rows`` without the chats being deleted. */
export function withoutPendingDeletes(rows: readonly ChatRow[]): ChatRow[] {
  return rows.filter((row) => !pendingChatIds.has(row.chatId));
}
