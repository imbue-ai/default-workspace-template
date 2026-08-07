/**
 * Renders the queued-message group: the messages the harness has parked while the
 * agent is mid-turn, under a subtle header row.
 *
 * The frontend is dumb here -- it renders a full snapshot the backend pushes on
 * the agents WebSocket (``AgentState.queued_messages``) and holds no queued state
 * of its own. The only action on the group is [Shoulder tap] (flush): it fires an
 * intent (`POST /flush-queue`) and paints nothing locally; the next snapshot
 * reflects what it did. (Interrupt-to-composer now lives on the composer's Stop
 * button -- see MessageInput.) There is no harness branch anywhere in here.
 */

import m from "mithril";
import { getQueuedMessagesForAgent } from "../models/AgentManager";
import type { QueuedMessage } from "../models/AgentManager";
import { flushQueue } from "../models/Response";
import { describeRequestError } from "../models/request-error";

const SHOULDER_TAP_TOOLTIP = "Gently interrupt model to send queued messages";

// Agents with the flush action in flight. While it runs the button is disabled so
// it cannot double-fire; cleared when the request settles. The snapshot itself
// stays the source of truth for what is shown.
const inFlightAgentIds = new Set<string>();

async function flushQueuedMessages(agentId: string): Promise<void> {
  if (inFlightAgentIds.has(agentId)) {
    return;
  }
  inFlightAgentIds.add(agentId);
  m.redraw();
  try {
    await flushQueue(agentId);
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
 *   'Queued messages' .................................... [Shoulder tap]
 * with the label on the left and the flush button (disabled while in flight,
 * with the shoulder-tap tooltip) on the right; the queued bubbles follow below.
 */
export function renderQueuedMessages(agentId: string): m.Vnode[] {
  const queued = getQueuedMessagesForAgent(agentId);
  if (queued.length === 0) {
    return [];
  }
  const isInFlight = inFlightAgentIds.has(agentId);

  const header = m("div", { class: "queued-header", key: "queued-header" }, [
    m("span", { class: "queued-header-label" }, "Queued messages"),
    m(
      "button",
      {
        type: "button",
        class: "queued-action queued-action--flush",
        disabled: isInFlight,
        // CSS tooltip (native title= is unreliable in the webview) -- same
        // data-tooltip pattern the progress-view markers use.
        "data-tooltip": SHOULDER_TAP_TOOLTIP,
        "aria-label": SHOULDER_TAP_TOOLTIP,
        onclick: () => flushQueuedMessages(agentId),
      },
      "Shoulder tap",
    ),
  ]);

  return [m("div", { class: "queued-group", key: "queued-group" }, [header, ...queued.map(renderQueuedBubble)])];
}
