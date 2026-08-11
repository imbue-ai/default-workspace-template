/**
 * Renders the queued-message group: the messages the harness has parked while the
 * agent is mid-turn, under a subtle header row.
 *
 * The frontend is dumb here -- it renders a full snapshot the backend pushes on
 * the agents WebSocket (``AgentState.queued_messages``) and holds no queued state
 * of its own. The only action on the group is [Shoulder tap] (flush): it fires an
 * intent (`POST /flush-queue`) and paints nothing locally -- the next backend queue
 * snapshot and the committed turn reflect the result. Whether the tap is available
 * is a backend decision; the frontend only greys the button while its own request is
 * in flight, so a click cannot double-fire.
 * (Interrupt-to-composer lives on the composer's Stop button -- see MessageInput.)
 * There is no harness branch anywhere in here.
 */

import m from "mithril";
import { getAgentById, getQueuedMessagesForAgent } from "../models/AgentManager";
import type { QueuedMessage } from "../models/AgentManager";
import { getHarnessCatalog } from "../models/HarnessCatalog";
import { flushQueue, shoulderTapAtomic } from "../models/Response";
import { describeRequestError } from "../models/request-error";

const SHOULDER_TAP_TOOLTIP = "Gently interrupt your agent to send queued messages early";
const QUEUED_INFO_TOOLTIP = "Messages below are sent when your agent takes a breather mid-work or finishes a turn.";

// Agents with the flush action in flight. While it runs the button is disabled so
// it cannot double-fire; cleared when the request settles. The snapshot itself
// stays the source of truth for what is shown.
const inFlightAgentIds = new Set<string>();

/** True when this agent's harness can flush the queue atomically (codex / pi / claude), so
 *  the "Shoulder tap" merges into the live turn instead of restarting-and-resending. */
function isAtomicShoulderTapAgent(agentId: string): boolean {
  const catalog = getHarnessCatalog(getAgentById(agentId)?.harness);
  return catalog?.native_atomic_shoulder_tap_possible === true;
}

async function flushQueuedMessages(agentId: string): Promise<void> {
  // Never flush while the tap is already running. The button is greyed while it is,
  // but ``disabled`` only takes effect on the next redraw, so a click can beat it --
  // this synchronous re-check at click time is the actual double-fire guard.
  if (inFlightAgentIds.has(agentId)) {
    return;
  }
  const isAtomic = isAtomicShoulderTapAgent(agentId);
  inFlightAgentIds.add(agentId);
  m.redraw();
  try {
    // The native harnesses (codex / pi / claude) flush into the live turn without a
    // restart; the rest restart and resend. The flag comes from the agent's harness
    // catalog. Either way the frontend paints nothing local: the next backend queue
    // snapshot and the committed turn reflect the result.
    if (isAtomic) {
      await shoulderTapAtomic(agentId);
    } else {
      await flushQueue(agentId);
    }
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
 *  than the classifier -- a queued message is always shown verbatim. */
function renderQueuedBubble(queued: QueuedMessage): m.Vnode {
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
  // Grey the button only while this tap's own request is in flight, to prevent a
  // double-fire. Whether the tap is otherwise available (e.g. a message is still
  // Sending) is a backend decision -- the frontend does not compute it.
  const isInFlight = inFlightAgentIds.has(agentId);

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
        disabled: isInFlight,
        "data-tooltip": SHOULDER_TAP_TOOLTIP,
        "aria-label": SHOULDER_TAP_TOOLTIP,
        onclick: () => flushQueuedMessages(agentId),
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
