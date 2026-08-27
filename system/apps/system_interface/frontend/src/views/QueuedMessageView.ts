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
 */

import m from "mithril";
import { getQueuedMessagesForAgent, getShoulderTapAvailableForAgent } from "../models/AgentManager";
import type { QueuedMessage } from "../models/AgentManager";
import { shoulderTap } from "../models/Response";
import { hoverTooltipAttrs } from "./hoverTooltip";
import { prependToComposer } from "./MessageInput";
import { OUTGOING_ROW_CLASS, OUTGOING_STATUS_CLASS } from "./OutgoingMessageView";
import { describeRequestError } from "../models/request-error";
import { buttonClass } from "./primitives";

const SHOULDER_TAP_TOOLTIP = "Gently interrupt your agent to send queued messages early";
const QUEUED_INFO_TOOLTIP = "Messages below are sent when your agent takes a breather mid-work or finishes a turn.";

// Agents with the shoulder-tap request in flight. While it runs the button is disabled so
// it cannot double-fire; cleared when the request settles. This is the ONLY thing the
// frontend tracks here -- whether the tap is otherwise available is the backend's flag.
const inFlightAgentIds = new Set<string>();

async function shoulderTapQueuedMessages(agentId: string): Promise<void> {
  // Never fire while our own tap is already running. The button is greyed while it is,
  // but ``disabled`` only takes effect on the next redraw, so a click can beat it --
  // this synchronous re-check at click time is the actual double-fire guard.
  if (inFlightAgentIds.has(agentId)) {
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
    alert(`Failed to send queued messages: ${detail}`);
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
      m("div", { class: "message-user-bubble" }, [
        m("div", { class: "message-content whitespace-pre-wrap" }, queued.content),
      ]),
      m("div", { class: OUTGOING_STATUS_CLASS }, "Sending…"),
    ]);
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
export function renderQueuedMessages(agentId: string): m.Vnode[] {
  const queued = getQueuedMessagesForAgent(agentId);
  if (queued.length === 0) {
    return [];
  }
  // The button's enabled state = the backend's availability flag, AND-ed with the local
  // double-fire guard. The frontend computes nothing about availability itself: if the
  // backend says a send is in flight (or the queue is empty), ``shoulder_tap_available`` is
  // false and the button is greyed -- so it can never be pressed into a refusal/error.
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
      "button",
      {
        type: "button",
        class: buttonClass("secondary", { sm: true, extra: "queued-action queued-action--flush shrink-0" }),
        disabled: isDisabled,
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
