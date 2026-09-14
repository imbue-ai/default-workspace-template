/**
 * The messages the chat app holds while the chat switches harness, at the tail of the transcript.
 *
 * They come from the chat's snapshot (``handoff.held_sends``), not from the page: the confirming
 * message first, then anything sent since. Each renders as the not-yet-real user bubble the
 * optimistic "Sending…" overlay uses, captioned with the switch's phase instead, and stays until
 * the snapshot stops listing it, so a reloaded page shows them too.
 */

import m from "mithril";
import { getChatById } from "../models/Chats";
import { handoffPhaseText } from "./handoff-phase";
import { renderNotYetRealBubble } from "./OutgoingMessageView";

export function renderHeldSends(chatId: string): m.Vnode[] {
  const chat = getChatById(chatId);
  if (chat === undefined || chat.handoff === null) return [];
  const caption = handoffPhaseText(chat.handoff, chat.active_agent.harness);
  return chat.handoff.held_sends.map((held) =>
    renderNotYetRealBubble({
      key: `held-${held.message_id}`,
      content: held.text,
      caption,
      extraRowClass: "held-send",
    }),
  );
}
