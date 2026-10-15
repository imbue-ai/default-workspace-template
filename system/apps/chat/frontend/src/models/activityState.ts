/**
 * The backend's per-agent activity states (``ActivityState``), as the ``activity_state`` of a
 * chat's active agent carries them: IDLE, THINKING, TOOL_RUNNING and COMPACTING. The views read
 * the strings directly; this names the one several of them branch on.
 */

// The agent's context is being compacted. Wins over a turn in progress while it lasts.
export const COMPACTING_STATE = "COMPACTING";

const WORKING_ACTIVITY_STATES: ReadonlySet<string> = new Set(["THINKING", "TOOL_RUNNING", COMPACTING_STATE]);

/** Whether the server-derived activity state means the agent is busy with a turn or a compaction. */
export function isWorkingActivityState(state: string | null | undefined): boolean {
  return state !== null && state !== undefined && WORKING_ACTIVITY_STATES.has(state);
}
