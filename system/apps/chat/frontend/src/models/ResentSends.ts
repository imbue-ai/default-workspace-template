/**
 * The messages the chat app delivered by sending them a second time, whose first copy may have landed too.
 * The message's row says so in one line under it, and a second copy of it in the transcript folds into that
 * line rather than showing as a second bubble.
 *
 * The chat app keeps them (``resent_sends`` on the chat's snapshot), so every window shows the same, after a
 * reload too.
 */

import { getChatById } from "./Chats";
import { userTurnsHolding } from "./Response";
import type { TranscriptEvent } from "./Response";

export interface ResentTurns {
  /** The user turns that carry the "Sent again" line: the first copy of each resent send. */
  notedTurnIds: ReadonlySet<string>;
  /** Second copies of a resent send, folded into the first's line. */
  foldedTurnIds: ReadonlySet<string>;
}

/** Changes whenever the chat's resent sends do, for a view that caches what it built from them. */
export function getResentSendsKey(chatId: string): string {
  return JSON.stringify(getChatById(chatId)?.resent_sends?.map((send) => [send.sent_after, send.closed_at]) ?? []);
}

/** A turn with no timestamp counts as landing after every send. */
function turnSeconds(event: TranscriptEvent): number {
  const milliseconds = Date.parse(event.timestamp);
  return Number.isNaN(milliseconds) ? Number.POSITIVE_INFINITY : milliseconds / 1000;
}

/** Which of ``events``, the chat's transcript, carry the "Sent again" line, and which fold into it. */
export function resentTurnsIn(chatId: string, events: readonly TranscriptEvent[]): ResentTurns {
  const notedTurnIds = new Set<string>();
  const foldedTurnIds = new Set<string>();
  for (const send of getChatById(chatId)?.resent_sends ?? []) {
    const [first, second] = userTurnsHolding(events, send.text).filter((turn) => turnSeconds(turn) >= send.sent_after);
    if (first === undefined) {
      continue;
    }
    notedTurnIds.add(first.event_id);
    if (second !== undefined && (send.closed_at === null || turnSeconds(second) < send.closed_at)) {
      foldedTurnIds.add(second.event_id);
    }
  }
  return { notedTurnIds, foldedTurnIds };
}
