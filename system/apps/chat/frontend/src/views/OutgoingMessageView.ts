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
import { getOutgoingMessages } from "../models/OutgoingMessages";
import type { OutgoingMessage } from "../models/OutgoingMessages";
import { USER_BUBBLE_CLASS, USER_MESSAGE_ROW_CLASS } from "./user-message-display";

// Shared with QueuedMessageView's is_sending branch, so an in-flight queued
// message and an optimistic outgoing one render identically. Composes the user
// rail's shared recipes: the dimming rides the row (opacity-60, and no bottom
// margin -- the caption is the tail of the group), the dashed not-yet-real
// border rides the bubble.
export const OUTGOING_ROW_CLASS = `${USER_MESSAGE_ROW_CLASS} outgoing-message outgoing-message--sending opacity-60`;

export const OUTGOING_BUBBLE_CLASS = `${USER_BUBBLE_CLASS} border border-dashed`;

export const OUTGOING_STATUS_CLASS = "outgoing-status mt-[3px] text-(length:--font-size-helper) text-secondary";

function renderOutgoingBubble(outgoing: OutgoingMessage): m.Vnode {
  return m(
    "div",
    {
      class: OUTGOING_ROW_CLASS,
      key: outgoing.id,
    },
    [
      m("div", { class: OUTGOING_BUBBLE_CLASS }, [
        m("div", { class: "message-content whitespace-pre-wrap" }, outgoing.content),
      ]),
      m("div", { class: OUTGOING_STATUS_CLASS }, "Sending…"),
    ],
  );
}

/** The optimistic outgoing bubbles for an agent, in send order. Returns [] when
 *  there are none. */
export function renderOutgoingMessages(agentId: string): m.Vnode[] {
  return getOutgoingMessages(agentId).map(renderOutgoingBubble);
}
