/**
 * Renders the optimistic "Sending…" bubbles (see models/OutgoingMessages) at the
 * very tail of the transcript -- below the committed turns AND below the queued
 * group, so a just-sent message shows immediately as the last thing(s) until the
 * harness-sourced state catches up. Reuses the plain user-bubble markup so an
 * outgoing message looks like the real one, with a small "Sending…" caption
 * beneath. A failed send is NOT rendered here -- the composer handles that (popup
 * + text restored), and the bubble is dropped.
 */
import m from "mithril";
import type { ChatId } from "../ids";
import { getOutgoingMessages } from "../models/OutgoingMessages";
import type { OutgoingMessage } from "../models/OutgoingMessages";

function renderOutgoingBubble(outgoing: OutgoingMessage): m.Vnode {
  return m(
    "div",
    {
      class: "message message-user outgoing-message outgoing-message--sending",
      key: outgoing.id,
    },
    [
      m("div", { class: "message-user-bubble" }, [
        m("div", { class: "message-content whitespace-pre-wrap" }, outgoing.content),
      ]),
      m("div", { class: "outgoing-status" }, "Sending…"),
    ],
  );
}

/** The optimistic outgoing bubbles for an agent, in send order. Returns [] when
 *  there are none. */
export function renderOutgoingMessages(chatId: ChatId): m.Vnode[] {
  return getOutgoingMessages(chatId).map(renderOutgoingBubble);
}
