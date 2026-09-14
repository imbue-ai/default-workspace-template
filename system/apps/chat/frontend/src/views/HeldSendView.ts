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
import { OUTGOING_BUBBLE_CLASS, OUTGOING_ROW_CLASS, OUTGOING_STATUS_CLASS } from "./OutgoingMessageView";

/** The message ids the chat app is holding for ``chatId``, so the page's own bubbles for them can stand down. */
export function heldSendMessageIds(chatId: string): Set<string> {
  return new Set((getChatById(chatId)?.handoff?.held_sends ?? []).map((held) => held.message_id));
}

export function renderHeldSends(chatId: string): m.Vnode[] {
  const chat = getChatById(chatId);
  if (chat === undefined || chat.handoff === null) return [];
  const caption = handoffPhaseText(chat.handoff, chat.active_agent.harness);
  return chat.handoff.held_sends.map((held) =>
    m("div", { class: `${OUTGOING_ROW_CLASS} held-send`, key: `held-${held.message_id}` }, [
      m("div", { class: OUTGOING_BUBBLE_CLASS }, [
        m("div", { class: "message-content whitespace-pre-wrap" }, held.text),
      ]),
      m("div", { class: OUTGOING_STATUS_CLASS }, caption),
    ]),
  );
}
