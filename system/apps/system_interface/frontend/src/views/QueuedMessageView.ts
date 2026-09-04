/**
 * Renders the queued-message group: the messages the harness has parked while the
 * agent is mid-turn, under a subtle header row.
 *
 * The frontend is dumb here -- it renders a full snapshot the backend pushes on
 * the agents WebSocket (``AgentState.queued_messages``) and holds no queued state
 * of its own. The only action on the group is [Shoulder tap]: it fires ONE
 * harness-agnostic intent (`POST /shoulder-tap-atomic`, which the backend dispatches
 * per harness) and paints nothing locally -- the next backend queue snapshot and the
 * committed turn reflect the result. Whether the tap is AVAILABLE is entirely the
 * backend's call, reported as ``shoulder_tap_available``; that flag alone greys the
 * button, so it can never be pressed in a state the backend would refuse (no error
 * path). The only thing the frontend adds is a local guard against double-firing its
 * own in-flight request.
 * (Interrupt-to-composer lives on the composer's Stop button -- see MessageInput.)
 * There is no harness branch anywhere in here.
 *
 * A published entry is not necessarily a PARKED one: the backend flags an entry it is
 * about to type, or is typing, as ``is_sending``, and such an entry renders as the
 * "Sending…" bubble rather than a queued chip. A snapshot that is entirely ``is_sending``
 * therefore gets no group chrome at all -- see ``renderQueuedMessages``.
 */

import m from "mithril";
import type { ChatId } from "../ids";
import { getQueuedMessagesForAgent, getShoulderTapAvailableForAgent } from "../models/AgentManager";
import type { QueuedMessage } from "../models/AgentManager";
import { shoulderTap } from "../models/Response";
import { prependToComposer, raiseFailureNotice } from "./MessageInput";
import { describeRequestError, describeRequestErrorKind } from "../models/request-error";

const SHOULDER_TAP_TOOLTIP = "Gently interrupt your agent to send queued messages early";
const QUEUED_INFO_TOOLTIP = "Messages below are sent when your agent takes a breather mid-work or finishes a turn.";

// Agents with the shoulder-tap request in flight. While it runs the button is disabled so
// it cannot double-fire; cleared when the request settles. This is the ONLY thing the
// frontend tracks here -- whether the tap is otherwise available is the backend's flag.
const inFlightAgentIds = new Set<string>();

async function shoulderTapQueuedMessages(chatId: ChatId): Promise<void> {
  // Never fire while our own tap is already running. The button is greyed while it is,
  // but ``disabled`` only takes effect on the next redraw, so a click can beat it --
  // this synchronous re-check at click time is the actual double-fire guard.
  if (inFlightAgentIds.has(chatId)) {
    return;
  }
  inFlightAgentIds.add(chatId);
  m.redraw();
  try {
    // One harness-agnostic call; the backend dispatches per harness. The frontend paints nothing
    // local: the next backend queue snapshot and the committed turn reflect the result. The one
    // exception is a non-empty ``block`` -- a native tap whose combined resend failed to submit hands
    // the parked text back for the composer (like Stop), so it is never swallowed (contract A1a).
    const { block } = await shoulderTap(chatId);
    prependToComposer(chatId, block);
  } catch (err) {
    const detail = describeRequestError(err);
    console.error(`Failed to send queued messages for chat ${chatId}: ${detail}`);
    // Hand the failure to the composer's notice rather than putting up a system alert. One shape
    // of failure gets one shape of answer, whichever button started it -- and Retry here means
    // "flush the queue again", which is exactly what the user clicked in the first place.
    raiseFailureNotice(chatId, {
      title: "Couldn't send the queued messages",
      detail,
      kind: describeRequestErrorKind(err),
      // The finally below has already cleared the in-flight marker by the time this can run.
      retry: () => shoulderTapQueuedMessages(chatId),
    });
  } finally {
    inFlightAgentIds.delete(chatId);
    m.redraw();
  }
}

/** Render one queued message as a user bubble. Reuses the user-bubble *view* (the
 *  same markup ``StableUserMessage`` produces for a plain prompt) directly, rather
 *  than the classifier -- a queued message is always shown verbatim.
 *
 *  A chip the backend reports as ``is_sending`` (a codex shoulder-tap's interrupt+resend)
 *  renders identically to the optimistic "Sending…" bubble (see OutgoingMessageView) -- same
 *  markup, same caption -- so a re-sent message stays continuously visible and reads "Sending…"
 *  through the resend rather than blinking out (contract A1a); the backend drives the transition
 *  to the committed turn. */
function renderQueuedBubble(queued: QueuedMessage): m.Vnode {
  if (queued.is_sending === true) {
    return m(
      "div",
      { class: "message message-user outgoing-message outgoing-message--sending", key: `queued-${queued.queued_id}` },
      [
        m("div", { class: "message-user-bubble" }, [
          m("div", { class: "message-content whitespace-pre-wrap" }, queued.content),
        ]),
        m("div", { class: "outgoing-status" }, "Sending…"),
      ],
    );
  }
  return m("div", { class: "message message-user queued-message", key: `queued-${queued.queued_id}` }, [
    m("div", { class: "message-user-bubble" }, [
      m("div", { class: "message-content whitespace-pre-wrap" }, queued.content),
    ]),
  ]);
}

/**
 * The queued group, rendered below the last committed turn. Returns [] when the
 * agent has nothing queued. A subtle header row reads:
 *   'Queued messages' (i) ................................... [Shoulder tap]
 * with the label + an info tooltip on the left and the flush button on the right;
 * the queued bubbles follow below. The group is a live mirror of the backend
 * snapshot -- it is never held or reconstructed on the frontend.
 */
export function renderQueuedMessages(chatId: ChatId): m.Vnode[] {
  const queued = getQueuedMessagesForAgent(chatId);
  if (queued.length === 0) {
    return [];
  }
  // Nothing is actually parked: every entry the backend published is one it is about to
  // type or is typing (agy's idle send, codex's shoulder-tap resend). Painting the
  // "Queued messages" header and the tap button over those tells the user a message is
  // WAITING when the backend is reporting the opposite -- and the header is the only
  // reason an idle send ever looked queued, since the bubbles themselves already render
  // as "Sending…". Bare bubbles, no group wrapper: identical markup to the optimistic
  // ones they replace, so the handoff is invisible rather than a reflow.
  if (queued.every((message) => message.is_sending === true)) {
    return queued.map((message) => renderQueuedBubble(message));
  }
  // The button's enabled state = the backend's availability flag, AND-ed with the local
  // double-fire guard. The frontend computes nothing about availability itself: if the
  // backend says a send is in flight (or the queue is empty), ``shoulder_tap_available`` is
  // false and the button is greyed -- so it can never be pressed into a refusal/error.
  const isInFlight = inFlightAgentIds.has(chatId);
  const isDisabled = isInFlight || !getShoulderTapAvailableForAgent(chatId);

  const header = m("div", { class: "queued-header", key: "queued-header" }, [
    m("span", { class: "queued-header-title" }, [
      m("span", { class: "queued-header-label" }, "Queued messages"),
      // A subtle (i) explaining when queued messages get sent. CSS tooltip (native
      // title= is unreliable in the webview), same data-tooltip pattern as the button.
      m(
        "span",
        {
          class: "queued-info",
          tabindex: 0,
          "data-tooltip": QUEUED_INFO_TOOLTIP,
          "aria-label": QUEUED_INFO_TOOLTIP,
        },
        "ⓘ",
      ),
    ]),
    m(
      "button",
      {
        type: "button",
        class: "queued-action queued-action--flush",
        disabled: isDisabled,
        "data-tooltip": SHOULDER_TAP_TOOLTIP,
        "aria-label": SHOULDER_TAP_TOOLTIP,
        onclick: () => shoulderTapQueuedMessages(chatId),
      },
      "Shoulder tap",
    ),
  ]);

  return [
    m("div", { class: "queued-group", key: "queued-group" }, [
      header,
      ...queued.map((message) => renderQueuedBubble(message)),
    ]),
  ];
}
