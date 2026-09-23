/**
 * Renders the optimistic outgoing bubbles (see models/OutgoingMessages) at the
 * very tail of the transcript -- below the committed turns AND below the queued
 * group, so a just-sent message shows immediately as the last thing(s) until the
 * harness-sourced state catches up. Reuses the plain user-bubble markup so an
 * outgoing message looks like the real one, faded until the real one replaces it.
 * A send still waiting for the agent to come up says so beside the model bar
 * (ConnectingIndicator), not here. A failed send is NOT rendered here -- the
 * composer handles that (popup + text restored), and the bubble is dropped.
 */
import m from "mithril";
import { getOutgoingMessages } from "../models/OutgoingMessages";
import type { OutgoingMessage } from "../models/OutgoingMessages";
import { USER_BUBBLE_CLASS, USER_MESSAGE_ROW_CLASS } from "./user-message-display";

// Composes the user rail's shared recipes: the dimming rides the row, the dashed
// not-yet-real border rides the bubble.
const OUTGOING_ROW_CLASS = `${USER_MESSAGE_ROW_CLASS} outgoing-message outgoing-message--sending opacity-60`;

// The committed user row's spacing, so consecutive not-yet-real bubbles stand apart and the
// real turn replaces one without a reflow.
const STANDALONE_ROW_SPACING_CLASS = "mb-5";

const OUTGOING_BUBBLE_CLASS = `${USER_BUBBLE_CLASS} border border-dashed`;

export interface NotYetRealBubble {
  key: string;
  /** The user's text, verbatim. */
  content: string;
  /** True inside a group whose own gap spaces its bubbles (the queued group), so the row
   *  carries no bottom margin of its own. */
  isGroupSpaced?: boolean;
  /** Marker classes added to the row, for a caller whose bubbles a test or a style picks out. */
  extraRowClass?: string;
}

/** A user message that is not yet a real turn, in the faded dashed bubble. Every not-yet-real
 *  message renders through here (optimistic, held by a switch, re-sent by a tap), so they look
 *  the same and the handoff between them is invisible. */
export function renderNotYetRealBubble(bubble: NotYetRealBubble): m.Vnode {
  const rowClass = [
    OUTGOING_ROW_CLASS,
    bubble.isGroupSpaced === true ? null : STANDALONE_ROW_SPACING_CLASS,
    bubble.extraRowClass ?? null,
  ]
    .filter((part) => part !== null)
    .join(" ");
  return m("div", { class: rowClass, key: bubble.key }, [
    m("div", { class: OUTGOING_BUBBLE_CLASS }, [
      m("div", { class: "message-content whitespace-pre-wrap" }, bubble.content),
    ]),
  ]);
}

function renderOutgoingBubble(outgoing: OutgoingMessage): m.Vnode {
  return renderNotYetRealBubble({ key: outgoing.id, content: outgoing.content });
}

/** The optimistic outgoing bubbles for a chat, in send order. Returns [] when
 *  there are none. */
export function renderOutgoingMessages(chatId: string): m.Vnode[] {
  return getOutgoingMessages(chatId).map(renderOutgoingBubble);
}
