/**
 * Renders the queued-message group: the messages the harness has parked while the
 * agent is mid-turn, under a subtle header row.
 *
 * The frontend is dumb here -- it renders a full snapshot the backend pushes on
 * the agents WebSocket (``AgentState.queued_messages``) and holds no queued state
 * of its own. The only action on the group is [Shoulder tap] (flush): it fires an
 * intent (`POST /flush-queue`) and paints nothing locally EXCEPT a transient
 * "freeze" so the group does not blip out while the agent restarts -- the freeze is
 * released by the same backend-arrival signal that clears a "Sending…" bubble (see
 * models/OutgoingMessages), so it clears exactly as the resent messages land.
 * (Interrupt-to-composer lives on the composer's Stop button -- see MessageInput.)
 * There is no harness branch anywhere in here.
 */

import m from "mithril";
import { getAgentById, getQueuedMessagesForAgent } from "../models/AgentManager";
import type { QueuedMessage } from "../models/AgentManager";
import { getHarnessCatalog } from "../models/HarnessCatalog";
import { getFlushFreeze, releaseFlushFreeze, startFlushFreeze } from "../models/OutgoingMessages";
import { flushQueue, shoulderTapAtomic } from "../models/Response";
import { requestTailFollow } from "../models/tailFollowRequest";
import { describeRequestError } from "../models/request-error";

const SHOULDER_TAP_TOOLTIP = "Gently interrupt your agent to send queued messages early";
const QUEUED_INFO_TOOLTIP = "Messages below are sent when your agent takes a breather mid-work or finishes a turn.";

// Agents with the flush action in flight. While it runs the button is disabled so
// it cannot double-fire; cleared when the request settles. The snapshot itself
// stays the source of truth for what is shown.
const inFlightAgentIds = new Set<string>();

/** True when this agent's harness can flush the queue atomically (codex), so the
 *  "Shoulder tap" merges into the live turn instead of restarting-and-resending. */
function isAtomicShoulderTapAgent(agentId: string): boolean {
  const catalog = getHarnessCatalog(getAgentById(agentId)?.harness);
  return catalog?.native_atomic_shoulder_tap_possible === true;
}

async function flushQueuedMessages(agentId: string): Promise<void> {
  // Snap the transcript to the bottom so the user follows the interrupted/merged turn
  // as it lands, even if they'd scrolled up while it was working.
  requestTailFollow(agentId);
  if (inFlightAgentIds.has(agentId)) {
    return;
  }
  const isAtomic = isAtomicShoulderTapAgent(agentId);
  // Capture the messages BEFORE the backend snapshot empties, and hold them greyed
  // until a backend arrival releases the freeze (arrival-driven, like a "Sending…"
  // bubble; a 20s cap is the only fallback). We deliberately do NOT release on the
  // POST resolving -- that would clear the hold before the merged/resent turn renders,
  // reopening the blip. The arrival is the release. This holds for both paths: the
  // restart-based flush and the atomic tap both empty the queue snapshot.
  const captured = getQueuedMessagesForAgent(agentId);
  inFlightAgentIds.add(agentId);
  if (captured.length > 0) {
    startFlushFreeze(agentId, captured);
  }
  m.redraw();
  try {
    // Codex merges the queue into the live turn without a restart; the other harnesses
    // restart and resend. The flag comes from the agent's harness catalog.
    await (isAtomic ? shoulderTapAtomic(agentId) : flushQueue(agentId));
  } catch (err) {
    const detail = describeRequestError(err);
    console.error(`Failed to send queued messages for agent ${agentId}: ${detail}`);
    // The flush failed -- drop the hold so the UI reverts to the true backend state,
    // and surface a popup.
    releaseFlushFreeze(agentId);
    alert(`Failed to send queued messages: ${detail}`);
  } finally {
    inFlightAgentIds.delete(agentId);
    m.redraw();
  }
}

/** Render one queued message as a user bubble. Reuses the user-bubble *view* (the
 *  same markup ``StableUserMessage`` produces for a plain prompt) directly, rather
 *  than the classifier -- a queued message is always shown verbatim. */
function renderQueuedBubble(queued: QueuedMessage, frozen = false): m.Vnode {
  const cls = frozen
    ? "message message-user queued-message queued-message--frozen"
    : "message message-user queued-message";
  return m("div", { class: cls, key: `queued-${queued.queued_id}` }, [
    m("div", { class: "message-user-bubble" }, [
      m("div", { class: "message-content whitespace-pre-wrap" }, queued.content),
    ]),
  ]);
}

/**
 * The queued group, rendered below the last committed turn. Returns [] when the
 * agent has nothing queued (and is not mid-flush). A subtle header row reads:
 *   'Queued messages' (i) ................................... [Shoulder tap]
 * with the label + an info tooltip on the left and the flush button on the right;
 * the queued bubbles follow below. While a flush restarts the agent, the group is
 * held greyed (no button) until the resent messages arrive.
 */
export function renderQueuedMessages(agentId: string): m.Vnode[] {
  // While a flush is restarting the agent, render the frozen (captured) messages
  // greyed, ignoring the backend snapshot (which briefly empties). The freeze is
  // released by a backend arrival (see models/OutgoingMessages), so it clears
  // exactly as the resent messages land. No visible countdown.
  const freeze = getFlushFreeze(agentId);
  if (freeze !== undefined) {
    const frozenHeader = m("div", { class: "queued-header queued-header--frozen", key: "queued-header" }, [
      m("span", { class: "queued-header-label" }, "Sending queued messages…"),
    ]);
    return [
      m("div", { class: "queued-group queued-group--frozen", key: "queued-group" }, [
        frozenHeader,
        ...freeze.messages.map((message) => renderQueuedBubble(message, true)),
      ]),
    ];
  }

  const queued = getQueuedMessagesForAgent(agentId);
  if (queued.length === 0) {
    return [];
  }
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
