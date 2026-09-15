/**
 * The messages the chat app holds while the chat switches harness, at the tail of the transcript.
 *
 * They come from the chat's snapshot (``handoff.held_sends``), not from the page: the confirming
 * message first, then anything sent since. Each renders as the not-yet-real user bubble the
 * optimistic "Sending…" overlay uses, with the same caption: the switch's own progress is the
 * handoff node's to tell (``handoff-node.ts``). They stay until the snapshot stops listing them,
 * so a reloaded page shows them too.
 */

import m from "mithril";
import { getChatById } from "../models/Chats";
import { renderNotYetRealBubble } from "./OutgoingMessageView";

export function renderHeldSends(chatId: string): m.Vnode[] {
  const chat = getChatById(chatId);
  if (chat === undefined || chat.handoff === null) return [];
  return chat.handoff.held_sends.map((held) =>
    renderNotYetRealBubble({
      key: `held-${held.message_id}`,
      content: held.text,
      caption: "Sending…",
      extraRowClass: "held-send",
    }),
  );
}
