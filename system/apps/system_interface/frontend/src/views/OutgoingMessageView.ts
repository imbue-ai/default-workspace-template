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

// Shared with QueuedMessageView's is_sending branch, so an in-flight queued
// message and an optimistic outgoing one render identically. The dimming rides
// the row (opacity-60); the bubble's dashed border and the zeroed row margin
// stay contextual `.outgoing-message` rules in style.css (the margin override
// must outrank the unlayered `.message` rhythm rule, which a utility cannot).
export const OUTGOING_ROW_CLASS = "message message-user outgoing-message outgoing-message--sending opacity-60";

export const OUTGOING_STATUS_CLASS = "outgoing-status mt-[3px] text-(length:--font-size-helper) text-secondary";

function renderOutgoingBubble(outgoing: OutgoingMessage): m.Vnode {
  return m(
    "div",
    {
      class: OUTGOING_ROW_CLASS,
      key: outgoing.id,
    },
    [
      m("div", { class: "message-user-bubble" }, [
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
