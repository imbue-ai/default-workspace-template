/**
 * The messages the chat app holds while the chat switches harness, at the tail of the transcript.
 *
 * They come from the chat's snapshot (``handoff.held_sends``), not from the page: the confirming
 * message first, then anything sent since. Each renders as the row the optimistic outgoing overlay
 * uses (the user bubble, or the notice a background-task report becomes), drawn as an ordinary
 * send: the switch's own progress is the handoff node's to tell (``handoff-node.ts``). The snapshot
 * keeps listing a send the switch has delivered until the switch is done, so a reloaded page shows
 * them too; each stands down earlier once its real form is on the page: the confirming message once
 * the switch marker that carries it is (the successor's opening turn), and the delivered others,
 * oldest first, as the agent's turns for them land or queue (``heldSendsShownElsewhere``). A send the
 * switch still holds always shows.
 */

import m from "mithril";
import { getChatById, getProvisionalChat } from "../models/Chats";
import { countHeldSendsShownElsewhere, getOutgoingMessages, heldSendsStillToShow } from "../models/OutgoingMessages";
import { countSentTurnsSince, isMessageCarriedBySwitch } from "../models/Response";
import { renderNotYetRealBubble } from "./OutgoingMessageView";

/** How many of a switching chat's delivered held sends the page already shows in their real form: the user turns its
 *  agent has taken since the switch began, in the transcript or in the agent's queue. Read from what any page loads,
 *  so a page opened partway through the switch counts the same; 0 while the chat is not switching. */
export function heldSendsShownElsewhere(chatId: string): number {
  const chat = getChatById(chatId);
  if (chat === undefined || chat.handoff === null) return 0;
  return countHeldSendsShownElsewhere(chat, chat.handoff.started_at, countSentTurnsSince);
}

export function renderHeldSends(chatId: string): m.Vnode[] {
  const chat = getChatById(chatId);
  if (chat === undefined || chat.handoff === null) return [];
  const standing = heldSendsStillToShow(
    chat.handoff.held_sends.filter((held) => !isMessageCarriedBySwitch(chatId, held.message_id)),
    heldSendsShownElsewhere(chatId),
  );
  return standing.map((held) =>
    renderNotYetRealBubble({
      key: `held-${held.message_id}`,
      content: held.text,
      extraRowClass: "held-send",
      displayDecision: held,
    }),
  );
}

/** The messages the chat app holds for a chat still being created (or whose create failed), at the tail of its
 *  empty conversation. This page's own bubble for one of them stands in its place, so a page that sent it shows
 *  it once; any other page, or this one reloaded, shows it from the record. */
export function renderProvisionalHeldSends(chatId: string): m.Vnode[] {
  const provisional = getProvisionalChat(chatId);
  if (provisional === undefined) return [];
  const ownBubbleIds = new Set(getOutgoingMessages(chatId).map((outgoing) => outgoing.messageId));
  return provisional.held_sends
    .filter((held) => !ownBubbleIds.has(held.message_id))
    .map((held) =>
      renderNotYetRealBubble({ key: `held-${held.message_id}`, content: held.text, extraRowClass: "held-send" }),
    );
}
