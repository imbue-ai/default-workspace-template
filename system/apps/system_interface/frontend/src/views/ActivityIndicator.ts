/**
 * Activity strip that sits just above the message input -- the harness-common shell.
 *
 * The backend (system interface) is the source of truth for *which* state the agent
 * is in -- IDLE / THINKING / TOOL_RUNNING -- delivered on ``activity_state`` via the
 * ``agents_updated`` WS payload. This component's job is to render a label:
 *   - IDLE / null      -> hidden
 *   - THINKING         -> "Thinking…"
 *   - TOOL_RUNNING     -> the in-flight tool call, captioned by the agent's harness
 *
 * The TOOL_RUNNING caption is read straight off the tool call: the harness's own
 * parser labelled it, so this view needs no notion of which harness is running.
 * A null ``activity_state`` means the server has no per-agent activity tracking
 * for this agent (proto-agents, remote agents) -- the strip collapses.
 */

import m from "mithril";
import type { ToolCall, TranscriptEvent } from "../models/Response";
import { getAgentById } from "../models/AgentManager";

/**
 * Find the most recent assistant tool call whose tool_call_id has no matching
 * tool_result event. Returns null if none. (Harness-agnostic: both parsers emit the
 * same ``assistant_message`` / ``tool_result`` shape.)
 */
function pendingToolCall(events: TranscriptEvent[]): ToolCall | null {
  const resolved = new Set<string>();
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "tool_result" && e.tool_call_id) {
      resolved.add(e.tool_call_id);
      continue;
    }
    if (e.type === "assistant_message" && e.tool_calls && e.tool_calls.length > 0) {
      for (let j = e.tool_calls.length - 1; j >= 0; j--) {
        const tc = e.tool_calls[j];
        if (!resolved.has(tc.tool_call_id)) {
          return tc;
        }
      }
    }
  }
  return null;
}

// Activity states in which the agent has an interruptible turn in progress.
const WORKING_ACTIVITY_STATES: ReadonlySet<string> = new Set(["THINKING", "TOOL_RUNNING"]);

/**
 * Whether the given server-derived activity state means the agent is in the
 * middle of an interruptible turn. Drives the visibility of the stop button.
 */
export function isWorkingActivityState(state: string | null | undefined): boolean {
  return state !== null && state !== undefined && WORKING_ACTIVITY_STATES.has(state);
}

/** The in-flight tool caption, as labelled by the harness that produced the call. */
function labelForToolCall(tc: ToolCall): string {
  return tc.caption_label || "Running tool…";
}

/**
 * Pick the user-facing label for a server-derived activity state. For TOOL_RUNNING
 * we consult the transcript for the in-flight tool and use its label; every other
 * state is fixed (or null = hide).
 */
export function labelForActivityState(state: string | null | undefined, events: TranscriptEvent[]): string | null {
  if (state === null || state === undefined) return null;
  if (state === "IDLE") return null;
  if (state === "THINKING") return "Thinking…";
  if (state === "TOOL_RUNNING") {
    const pending = pendingToolCall(events);
    if (pending !== null) return labelForToolCall(pending);
    return "Running tool…";
  }
  return null;
}

function renderStrip(label: string, state: string | null | undefined): m.Vnode {
  return m("div.agent-activity-indicator", { "data-state": state, role: "status", "aria-live": "polite" }, [
    m("span.agent-activity-indicator__dot"),
    m("span.agent-activity-indicator__label", label),
  ]);
}

// Minimum time a "Running X" caption stays up. A tool call that finishes in a
// fraction of a second would otherwise flash past unreadably -- common for codex,
// whose code-mode calls often yield immediately, but claude has fast tools too, so
// this is about tool duration rather than about which harness is running. The hold
// applies ONLY while the agent is still working (THINKING); the turn ending (IDLE)
// clears it at once, so the indicator never lingers past when it should go away.
const TOOL_CAPTION_MIN_MS = 700;

interface ActivityIndicatorAttrs {
  agentId: string;
  events: TranscriptEvent[];
}

export function ActivityIndicator(): m.Component<ActivityIndicatorAttrs> {
  // Per-mounted-panel (i.e. per-agent) debounce state for the tool caption.
  let heldToolCaption: string | null = null;
  let heldUntil = 0;
  let releaseTimer: number | null = null;

  const cancelRelease = (): void => {
    if (releaseTimer !== null) {
      window.clearTimeout(releaseTimer);
      releaseTimer = null;
    }
  };

  return {
    view(vnode) {
      const { agentId, events } = vnode.attrs;
      const state = getAgentById(agentId)?.activity_state ?? null;
      const label = labelForActivityState(state, events);

      const now = Date.now();
      if (state === "TOOL_RUNNING" && label !== null) {
        // Active tool -> (re)start the hold window; cancel any pending release.
        cancelRelease();
        heldToolCaption = label;
        heldUntil = now + TOOL_CAPTION_MIN_MS;
      } else if (state === "THINKING" && heldToolCaption !== null && now < heldUntil) {
        // Still working, but the tool cleared fast -- keep the caption up briefly so
        // it doesn't flash, then release. Schedule a redraw at the release point.
        if (releaseTimer === null) {
          releaseTimer = window.setTimeout(() => {
            releaseTimer = null;
            heldToolCaption = null;
            m.redraw();
          }, heldUntil - now);
        }
        return renderStrip(heldToolCaption, "TOOL_RUNNING");
      } else {
        // IDLE / null (turn ended), window expired, or nothing held -> release now.
        cancelRelease();
        heldToolCaption = null;
      }

      if (label === null) return null;
      return renderStrip(label, state);
    },
  };
}
