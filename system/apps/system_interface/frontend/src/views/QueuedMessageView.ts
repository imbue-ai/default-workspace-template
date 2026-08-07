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

// Hard cap on the freeze window. The freeze normally releases as soon as the flush
// POST settles (the /flush-queue endpoint restarts + resends and only returns once
// the backend STRICT-confirms delivery -- that HTTP response IS the success
// signal). This cap only bounds a request that never settles.
const FREEZE_CAP_MS = 10_000;
// A beat after the POST settles before unfreezing, so the resent committed turn's
// WS push renders before the greyed group vanishes (no empty flash).
const FREEZE_SETTLE_MS = 500;

// Agents with the flush action in flight. While it runs the button is disabled so
// it cannot double-fire; cleared when the request settles. The snapshot itself
// stays the source of truth for what is shown.
const inFlightAgentIds = new Set<string>();

// Frozen queued groups: while an agent restarts for a flush, the backend snapshot
// momentarily empties (its in-harness queue is killed) before the messages
// reappear as a committed turn. Rendering the live snapshot then would blip the
// messages out and back. Instead, on click we capture the current messages and
// render THEM (greyed, with a countdown) until the flush POST settles -- a purely
// client-side, ephemeral hold that dies with the tab.
interface FrozenGroup {
  messages: QueuedMessage[];
  deadline: number; // Date.now() cap, drives the countdown
}
const freezeByAgent = new Map<string, FrozenGroup>();
let countdownTimer: ReturnType<typeof setInterval> | null = null;

function ensureCountdownTicking(): void {
  if (countdownTimer === null && freezeByAgent.size > 0) {
    countdownTimer = setInterval(() => m.redraw(), 1000);
  }
}

function releaseFreeze(agentId: string): void {
  const removed = freezeByAgent.delete(agentId);
  if (freezeByAgent.size === 0 && countdownTimer !== null) {
    clearInterval(countdownTimer);
    countdownTimer = null;
  }
  if (removed) {
    m.redraw();
  }
}

async function flushQueuedMessages(agentId: string): Promise<void> {
  if (inFlightAgentIds.has(agentId)) {
    return;
  }
  // Capture the messages BEFORE the restart empties the backend snapshot.
  const captured = getQueuedMessagesForAgent(agentId);
  inFlightAgentIds.add(agentId);
  if (captured.length > 0) {
    freezeByAgent.set(agentId, { messages: captured, deadline: Date.now() + FREEZE_CAP_MS });
    ensureCountdownTicking();
    // Fallback: never hold the freeze past the cap even if the POST never settles.
    setTimeout(() => releaseFreeze(agentId), FREEZE_CAP_MS);
  }
  m.redraw();
  try {
    await flushQueue(agentId);
    // Settled: the backend restarted and confirmed the resent turn. Give its WS
    // push a beat to render the committed turn, then drop the greyed hold.
    setTimeout(() => releaseFreeze(agentId), FREEZE_SETTLE_MS);
  } catch (err) {
    const detail = describeRequestError(err);
    console.error(`Failed to send queued messages for agent ${agentId}: ${detail}`);
    // Nothing happened -- drop the hold immediately so the real (unchanged) state
    // shows again, and surface the failure.
    releaseFreeze(agentId);
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
 * agent has nothing queued. A subtle header row reads:
 *   'Queued messages' .................................... [Shoulder tap]
 * with the label on the left and the flush button (disabled while in flight,
 * with the shoulder-tap tooltip) on the right; the queued bubbles follow below.
 */
export function renderQueuedMessages(agentId: string): m.Vnode[] {
  // While a flush is restarting the agent, render the frozen (captured) messages
  // greyed with a countdown, ignoring the backend snapshot (which briefly empties)
  // -- this is the fix for the messages blipping out during the restart.
  const freeze = freezeByAgent.get(agentId);
  if (freeze !== undefined) {
    const remaining = Math.max(0, Math.ceil((freeze.deadline - Date.now()) / 1000));
    const frozenHeader = m("div", { class: "queued-header queued-header--frozen", key: "queued-header" }, [
      m("span", { class: "queued-header-label" }, "Sending queued messages…"),
      m("span", { class: "queued-countdown" }, `${remaining}s`),
    ]);
    return [
      m("div", { class: "queued-group queued-group--frozen", key: "queued-group" }, [
        frozenHeader,
        ...freeze.messages.map((queued) => renderQueuedBubble(queued, true)),
      ]),
    ];
  }

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

  return [
    m("div", { class: "queued-group", key: "queued-group" }, [
      header,
      ...queued.map((message) => renderQueuedBubble(message)),
    ]),
  ];
}
