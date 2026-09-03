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
 *
 * One state has no backend signal at all: the beat between the user resolving a
 * permission request and the agent being told. The verdict reaches this page
 * over the embed contract the moment the user clicks (see `permission-card.ts`),
 * but the agent only resumes once the desktop client's retried `mngr message`
 * delivery actually lands the notice in its session -- until then the agent is
 * genuinely IDLE and the strip would show nothing, so the user sees their
 * decision land and then apparent silence. Through that gap the strip shows a
 * bare dot with no caption (there is nothing truthful to say yet), bounded by
 * `WAKE_SPINNER_MS`.
 */

import m from "mithril";
import type { ToolCall, TranscriptEvent } from "../models/Response";
import { getAgentById } from "../models/AgentManager";
import { resolutionRequestIdOf } from "./message-classification";
import { hasShellResolutionSince, shellResolutionArrivalFor } from "./permission-card";

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

// What the captionless wake-up dot is called for anyone who cannot see it (and
// for anyone who hovers it). Not rendered as visible text: the whole point of
// this state is that we do not yet know what the agent will do with the
// verdict, so a visible caption would be inventing detail.
const WAKE_DESCRIPTION = "Passing your decision to the agent";

// Marks the wake-up strip for styling and tests. Deliberately not one of the
// server's activity states -- the server has no signal for this beat.
const WAKE_STATE = "WAKING";

function renderStrip(label: string | null, state: string | null | undefined): m.Vnode {
  const attrs: Record<string, unknown> = { "data-state": state, role: "status", "aria-live": "polite" };
  if (label === null) {
    attrs["aria-label"] = WAKE_DESCRIPTION;
    attrs["title"] = WAKE_DESCRIPTION;
  }
  return m("div.agent-activity-indicator", attrs, [
    m("span.agent-activity-indicator__dot"),
    label === null ? null : m("span.agent-activity-indicator__label", label),
  ]);
}

// How long the bare dot may stand in for the agent after a verdict. Long enough
// to cover an ordinary delivery (discovery, the TUI paste-and-confirm
// handshake, and the first retry or two), short enough that a delivery which
// never lands leaves the chat honestly idle rather than spinning forever.
const WAKE_SPINNER_MS = 10_000;

/**
 * When the wake-up dot should stop, or null when it should not be shown.
 *
 * The dot is shown for a request meeting all three of:
 *   (a) THIS agent filed it -- verdicts are recorded page-wide, so a sibling
 *       panel's request must not spin this panel;
 *   (b) the shell reported its verdict within the last `WAKE_SPINNER_MS`;
 *   (c) the transcript does not yet carry its resolution notice -- i.e. the
 *       agent still has not been told.
 *
 * (c) is what keeps a reloaded page quiet: the load-time snapshot re-reports
 * verdicts the agent heard about long ago, and every one of those already has
 * its notice in the transcript.
 *
 * Returns the latest such deadline, so a second verdict resolved during the
 * window extends the dot rather than truncating it.
 */
export function wakeUpSpinnerDeadline(events: TranscriptEvent[], now: number): number | null {
  const since = now - WAKE_SPINNER_MS;
  // Cheap gate: no verdict landed recently, so no scan is worth doing.
  if (!hasShellResolutionSince(since)) return null;

  const filedHere = new Set<string>();
  const announcedHere = new Set<string>();
  for (const e of events) {
    if (e.type === "tool_result") {
      // The backend parses the gateway's echoed object off the untruncated tool
      // output onto `permission_request`; its `request_id` is the same id the
      // shell's verdicts are keyed by.
      const requestId = e.permission_request?.request_id;
      if (typeof requestId === "string" && requestId !== "") filedHere.add(requestId);
      continue;
    }
    if (e.type === "user_message") {
      const requestId = resolutionRequestIdOf(e);
      if (requestId !== null) announcedHere.add(requestId);
    }
  }

  let deadline: number | null = null;
  for (const requestId of filedHere) {
    if (announcedHere.has(requestId)) continue;
    const arrival = shellResolutionArrivalFor(requestId);
    if (arrival === null || arrival <= since) continue;
    const candidate = arrival + WAKE_SPINNER_MS;
    if (deadline === null || candidate > deadline) deadline = candidate;
  }
  return deadline;
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
  // Fires at the wake-up dot's deadline: nothing else would redraw this panel
  // when the window simply runs out (the agent never arriving is, by
  // definition, the absence of an event).
  let wakeTimer: number | null = null;

  const cancelRelease = (): void => {
    if (releaseTimer !== null) {
      window.clearTimeout(releaseTimer);
      releaseTimer = null;
    }
  };

  const cancelWake = (): void => {
    if (wakeTimer !== null) {
      window.clearTimeout(wakeTimer);
      wakeTimer = null;
    }
  };

  return {
    onremove() {
      cancelRelease();
      cancelWake();
    },
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
        cancelWake();
        return renderStrip(heldToolCaption, "TOOL_RUNNING");
      } else {
        // IDLE / null (turn ended), window expired, or nothing held -> release now.
        cancelRelease();
        heldToolCaption = null;
      }

      if (label === null) {
        // Nothing else is going on, so this is the only moment the wake-up dot
        // may take the strip -- a real turn always outranks it.
        const wakeDeadline = wakeUpSpinnerDeadline(events, now);
        if (wakeDeadline !== null) {
          if (wakeTimer === null) {
            wakeTimer = window.setTimeout(() => {
              wakeTimer = null;
              m.redraw();
            }, wakeDeadline - now);
          }
          return renderStrip(null, WAKE_STATE);
        }
        cancelWake();
        return null;
      }
      cancelWake();
      return renderStrip(label, state);
    },
  };
}
