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
 * backend's call, reported as ``shoulder_tap_available``; that flag greys the button
 * (aria-disabled, so the explanatory tooltip keeps working) and gates the click
 * handler, so it can never be pressed in a state the backend would refuse (no error
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
import { getQueuedMessagesForAgent, getShoulderTapAvailableForAgent } from "../models/AgentManager";
import type { QueuedMessage } from "../models/AgentManager";
import { shoulderTap } from "../models/Response";
import { hoverTooltipAttrs } from "./components/hoverTooltip";
import { prependToComposer, raiseFailureNotice } from "./MessageInput";
import { OUTGOING_BUBBLE_CLASS, OUTGOING_ROW_CLASS, OUTGOING_STATUS_CLASS } from "./OutgoingMessageView";
import { describeRequestError, describeRequestErrorKind } from "../models/request-error";
import { Button } from "./components/Button";
import { USER_BUBBLE_CLASS, USER_MESSAGE_ROW_CLASS } from "./user-message-display";

const SHOULDER_TAP_TOOLTIP = "Gently interrupt your agent to send queued messages early";
const QUEUED_INFO_TOOLTIP = "Messages below are sent when your agent takes a breather mid-work or finishes a turn.";

// Agents with the shoulder-tap request in flight. While it runs the button is greyed and
// the click gate refuses, so it cannot double-fire; cleared when the request settles. This is the ONLY thing the
// frontend tracks here -- whether the tap is otherwise available is the backend's flag.
const inFlightAgentIds = new Set<string>();

async function shoulderTapQueuedMessages(agentId: string): Promise<void> {
  // Never fire while our own tap is already running or the backend reports the tap
  // unavailable. The greyed button is aria-disabled (see the render site for why), so
  // clicks still arrive -- this synchronous check is the real gate, and it also
  // covers a click racing a redraw.
  if (inFlightAgentIds.has(agentId) || !getShoulderTapAvailableForAgent(agentId)) {
    return;
  }
  inFlightAgentIds.add(agentId);
  m.redraw();
  try {
    // One harness-agnostic call; the backend dispatches per harness. The frontend paints nothing
    // local: the next backend queue snapshot and the committed turn reflect the result. The one
    // exception is a non-empty ``block`` -- a native tap whose combined resend failed to submit hands
    // the parked text back for the composer (like Stop), so it is never swallowed (contract A1a).
    const { block } = await shoulderTap(agentId);
    prependToComposer(agentId, block);
  } catch (err) {
    const detail = describeRequestError(err);
    console.error(`Failed to send queued messages for agent ${agentId}: ${detail}`);
    // Hand the failure to the composer's notice rather than putting up a system alert. One shape
    // of failure gets one shape of answer, whichever button started it -- and Retry here means
    // "flush the queue again", which is exactly what the user clicked in the first place.
    raiseFailureNotice(agentId, {
      title: "Couldn't send the queued messages",
      detail,
      kind: describeRequestErrorKind(err),
      // The finally below has already cleared the in-flight marker by the time this can run.
      retry: () => shoulderTapQueuedMessages(agentId),
    });
  } finally {
    inFlightAgentIds.delete(agentId);
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
    return m("div", { class: OUTGOING_ROW_CLASS, key: `queued-${queued.queued_id}` }, [
      m("div", { class: OUTGOING_BUBBLE_CLASS }, [
        m("div", { class: "message-content whitespace-pre-wrap" }, queued.content),
      ]),
      m("div", { class: OUTGOING_STATUS_CLASS }, "Sending…"),
    ]);
  }
  // opacity-85: the not-yet-sent muting; no bottom margin (the group's own gap
  // is the rhythm between queued bubbles).
  return m("div", { class: `${USER_MESSAGE_ROW_CLASS} queued-message`, key: `queued-${queued.queued_id}` }, [
    m("div", { class: `${USER_BUBBLE_CLASS} opacity-85` }, [
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
export function renderQueuedMessages(agentId: string): m.Vnode[] {
  const queued = getQueuedMessagesForAgent(agentId);
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
  // backend says a send is in flight (or the queue is empty), ``shoulder_tap_available``
  // is false and the button is greyed. The greying is aria-disabled, not disabled: a
  // disabled button suppresses the hover/focus events the explanatory tooltip needs,
  // and the tooltip matters most exactly while the button is greyed; the click
  // handler re-checks this condition synchronously, so it can never be pressed into
  // a refusal/error.
  const isInFlight = inFlightAgentIds.has(agentId);
  const isDisabled = isInFlight || !getShoulderTapAvailableForAgent(agentId);

  // Header row spanning the message column's left..right bounds: the label (plus
  // its (i) info icon) left-aligned and allowed to shrink/ellipsize, the
  // [Shoulder tap] button pinned right and never crowded or shrunk.
  const header = m("div", { class: "queued-header flex items-center justify-between gap-3", key: "queued-header" }, [
    m("span", { class: "queued-header-title flex min-w-0 items-center gap-1.5" }, [
      m(
        "span",
        {
          class:
            "queued-header-label min-w-0 truncate text-(length:--font-size-helper) font-medium tracking-[0.02em] text-secondary",
        },
        "Queued messages",
      ),
      // A subtle (i) explaining when queued messages get sent. JS hover tooltip
      // (native title= is unreliable in the webview), same pattern as the button.
      m(
        "span",
        {
          class:
            "queued-info shrink-0 cursor-help text-(length:--font-size-helper) leading-none text-secondary opacity-70 hover:opacity-100",
          tabindex: 0,
          ...hoverTooltipAttrs(QUEUED_INFO_TOOLTIP),
          "aria-label": QUEUED_INFO_TOOLTIP,
        },
        "ⓘ",
      ),
    ]),
    m(
      Button,
      {
        sm: true,
        extra: "queued-action queued-action--flush shrink-0",
        ...(isDisabled ? { "aria-disabled": "true" } : {}),
        ...hoverTooltipAttrs(SHOULDER_TAP_TOOLTIP),
        "aria-label": SHOULDER_TAP_TOOLTIP,
        onclick: () => shoulderTapQueuedMessages(agentId),
      },
      "Shoulder tap",
    ),
  ]);

  return [
    m("div", { class: "queued-group mb-5 flex flex-col items-stretch gap-2", key: "queued-group" }, [
      header,
      ...queued.map((message) => renderQueuedBubble(message)),
    ]),
  ];
}
