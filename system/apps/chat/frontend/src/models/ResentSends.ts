/**
 * The sends the chat app delivered by sending them a second time (the message route's "resent"): mngr
 * could not confirm the first copy reached the agent, and the first may have landed too. The message's
 * row says so in one line under it, and a second copy of it in the transcript folds into that line
 * rather than showing as a second bubble.
 *
 * Kept by this page, in memory, from the send's answer: another window, or this one after a reload,
 * shows such a message as it would any other, a second copy included.
 */

import { addMessageSentListener, sentUserTurnIds, userTurnIdsLandedSince } from "./Response";
import type { TranscriptEvent } from "./Response";

interface ResentSend {
  /** What went to the agent, which its user turn holds. */
  text: string;
  /** The chat's user turns before the send, so only turns that landed after it count. */
  priorTurnIds: ReadonlySet<string>;
  /** The chat's user turns when this page next sent to it, or null before then: a copy landing after that
   *  may be the user's own, and is not folded. */
  turnIdsAtNextSend: ReadonlySet<string> | null;
}

export interface ResentTurns {
  /** The user turns that carry the "Sent again" line: the first copy of each resent send. */
  notedTurnIds: ReadonlySet<string>;
  /** Second copies of a resent send, folded into the first's line. */
  foldedTurnIds: ReadonlySet<string>;
}

const resentSendsByChat = new Map<string, ResentSend[]>();
const versionByChat = new Map<string, number>();
// Whether this page's sends are being heard, from the first resent send on.
let isHearingSends = false;

function bumpVersion(chatId: string): void {
  versionByChat.set(chatId, (versionByChat.get(chatId) ?? 0) + 1);
}

/** Changes whenever the chat's resent sends do, for a view that caches what it built from them. */
export function getResentSendsVersion(chatId: string): number {
  return versionByChat.get(chatId) ?? 0;
}

/** Note that ``text``, sent to ``chatId`` when its transcript held ``priorTurnIds``, was delivered by a second send. */
export function noteResentSend(chatId: string, text: string, priorTurnIds: ReadonlySet<string>): void {
  if (!isHearingSends) {
    addMessageSentListener(closeFoldsOnNextSend);
    isHearingSends = true;
  }
  resentSendsByChat.set(chatId, [
    ...(resentSendsByChat.get(chatId) ?? []),
    { text, priorTurnIds, turnIdsAtNextSend: null },
  ]);
  bumpVersion(chatId);
}

function closeFoldsOnNextSend(chatId: string): void {
  const sends = resentSendsByChat.get(chatId);
  if (sends === undefined || sends.every((send) => send.turnIdsAtNextSend !== null)) {
    return;
  }
  const turnIds = sentUserTurnIds(chatId);
  resentSendsByChat.set(
    chatId,
    sends.map((send) => (send.turnIdsAtNextSend === null ? { ...send, turnIdsAtNextSend: turnIds } : send)),
  );
  bumpVersion(chatId);
}

/** Which of ``events``, the chat's transcript, carry the "Sent again" line, and which fold into it. */
export function resentTurnsIn(chatId: string, events: readonly TranscriptEvent[]): ResentTurns {
  const notedTurnIds = new Set<string>();
  const foldedTurnIds = new Set<string>();
  for (const send of resentSendsByChat.get(chatId) ?? []) {
    const [first, second] = userTurnIdsLandedSince(events, send.text, send.priorTurnIds);
    if (first === undefined) {
      continue;
    }
    notedTurnIds.add(first);
    if (second !== undefined && (send.turnIdsAtNextSend === null || send.turnIdsAtNextSend.has(second))) {
      foldedTurnIds.add(second);
    }
  }
  return { notedTurnIds, foldedTurnIds };
}
