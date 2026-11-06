/**
 * The names the chat root shows for chats whose rename is on its way: the list, the phone header and the
 * shell's window title read the typed name the moment the field closes, while the chat app runs the rename
 * (an mngr rename, which takes seconds). The typed name holds until the chat app answers: an automatic name it
 * shows meanwhile does not replace it, since the chat app lets the person's rename land last. Once it accepts
 * the rename, its own name takes over with the first chat list after (it reports the new name before it
 * answers). A refused rename is dropped here, so the old name comes back with the refusal.
 */

import type { ChatRow } from "./rows";

interface PendingRename {
  title: string;
  isAccepted: boolean;
}

const pendingByChatId = new Map<string, PendingRename>();

/** Show ``title`` for ``chatId`` until the chat app answers the rename. */
export function showPendingRename(chatId: string, title: string): void {
  pendingByChatId.set(chatId, { title, isAccepted: false });
}

/** The chat app took the rename of ``chatId`` to ``title``: its next chat list carries the name. */
export function acceptPendingRename(chatId: string, title: string): void {
  const pending = pendingByChatId.get(chatId);
  if (pending !== undefined && pending.title === title) pending.isAccepted = true;
}

/** The chat app refused the rename of ``chatId`` to ``title``: the name it has stands. */
export function dropPendingRename(chatId: string, title: string): void {
  if (pendingByChatId.get(chatId)?.title === title) pendingByChatId.delete(chatId);
}

/** A chat list arrived: every accepted rename is in it, so the chat app's names take over for those. */
export function settleAcceptedRenames(): void {
  for (const [chatId, pending] of pendingByChatId) {
    if (pending.isAccepted) pendingByChatId.delete(chatId);
  }
}

/** ``rows`` with each pending rename's name in place of the chat app's. */
export function withPendingTitles(rows: readonly ChatRow[]): ChatRow[] {
  return rows.map((row) => {
    const pending = pendingByChatId.get(row.chatId);
    return pending === undefined ? row : { ...row, title: pending.title };
  });
}

/** The name to show for ``chatId`` whose chat app name is ``title``. */
export function titleWithPendingRename(chatId: string, title: string): string {
  return pendingByChatId.get(chatId)?.title ?? title;
}
