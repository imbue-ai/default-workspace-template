/**
 * Renders the optimistic "Sending…" bubbles (see models/OutgoingMessages) at the
 * very tail of the transcript -- below the committed turns AND below the queued
 * group, so a just-sent message shows immediately as the last thing(s) until the
 * harness-sourced state catches up. Reuses the plain user-bubble markup so an
 * outgoing message looks like the real one, with a small status caption beneath.
 */
import m from "mithril";
import { getOutgoingMessages } from "../models/OutgoingMessages";
import type { OutgoingMessage } from "../models/OutgoingMessages";

function renderOutgoingBubble(outgoing: OutgoingMessage): m.Vnode {
  const caption =
    outgoing.status === "failed" ? `Failed to send${outgoing.error ? `: ${outgoing.error}` : ""}` : "Sending…";
  return m(
    "div",
    {
      class: `message message-user outgoing-message outgoing-message--${outgoing.status}`,
      key: outgoing.id,
    },
    [
      m("div", { class: "message-user-bubble" }, [
        m("div", { class: "message-content whitespace-pre-wrap" }, outgoing.content),
      ]),
      m("div", { class: "outgoing-status" }, caption),
    ],
  );
}

/** The optimistic outgoing bubbles for an agent, in send order. Returns [] when
 *  there are none. */
export function renderOutgoingMessages(agentId: string): m.Vnode[] {
  return getOutgoingMessages(agentId).map(renderOutgoingBubble);
}
