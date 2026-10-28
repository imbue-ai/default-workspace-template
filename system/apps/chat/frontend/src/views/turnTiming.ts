/**
 * When the agent's current (or latest) turn started and ended, read off the transcript.
 *
 * A turn starts at the user message that opened it: any message that opens a section of the
 * progress timeline (the user's own words, a system chip the agent resumes after, a permission
 * verdict), not a skill expansion or a framework injection inside the turn. It ends at the agent's
 * last reply, tool call or tool result after that message -- not at a compaction that ran once the
 * agent was idle, which lands later without opening a turn. Both are the transcript's own
 * timestamps, so a reloaded page reads the same turn the same way.
 */

import type { TranscriptEvent } from "../models/Response";
import { isNonBoundaryUserMessage } from "./message-classification";

/** The span of the transcript's latest turn, in epoch milliseconds. */
export interface TurnSpan {
  /** The event id of the message that opened the turn. */
  openingEventId: string;
  startedAt: number;
  /** When the agent last recorded work in the turn; equal to `startedAt` while it has recorded none. */
  lastEventAt: number;
  /** The event id of the agent's last work in the turn (its reply, tool call or tool result), or null while it has recorded none. */
  lastWorkEventId: string | null;
  /** Whether anything of the agent's follows the opening message (a reply, a tool call or its result). */
  hasAgentWork: boolean;
}

/** The latest turn's span, or null when the transcript holds no turn-opening message or its timestamp does not parse. */
export function latestTurnSpan(events: readonly TranscriptEvent[]): TurnSpan | null {
  let lastWorkAt: number | null = null;
  let lastWorkEventId: string | null = null;
  let hasAgentWork = false;
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    const at = Date.parse(event.timestamp);
    if (event.type === "user_message" && !isNonBoundaryUserMessage(event)) {
      if (Number.isNaN(at)) return null;
      return {
        openingEventId: event.event_id,
        startedAt: at,
        lastEventAt: Math.max(at, lastWorkAt ?? at),
        lastWorkEventId,
        hasAgentWork,
      };
    }
    if (event.type === "assistant_message" || event.type === "tool_result") {
      hasAgentWork = true;
      lastWorkEventId ??= event.event_id;
      if (lastWorkAt === null && !Number.isNaN(at)) lastWorkAt = at;
    }
  }
  return null;
}

/** A duration as the status row and the done mark write it: "8s", "1m 05s", "1h 02m". */
export function formatElapsed(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const totalMinutes = Math.floor(totalSeconds / 60);
  if (totalMinutes < 60) return `${totalMinutes}m ${String(totalSeconds % 60).padStart(2, "0")}s`;
  return `${Math.floor(totalMinutes / 60)}h ${String(totalMinutes % 60).padStart(2, "0")}m`;
}
