/**
 * Whether an agent's mngr lifecycle state says its process is gone, for the views that act on
 * that (the terminal back face unmounts its iframe).
 *
 * This is distinct from the chat's activity indicator (THINKING / TOOL_RUNNING / IDLE), which
 * only describes work *within* a running process.
 */

// Lifecycle states in which the claude process is alive, whether working
// (RUNNING) or idle (WAITING). Outside this set the process is not running.
// RUNNING_UNKNOWN_AGENT_TYPE is an agent running under a process name mngr
// can't confirm; we treat it as running.
const ALIVE_STATES: ReadonlySet<string> = new Set(["RUNNING", "RUNNING_UNKNOWN_AGENT_TYPE", "WAITING"]);

/** True iff ``state`` POSITIVELY says the agent process is dead.
 *
 * UNKNOWN means the observer could not look (non-evidence, the agent may be running fine), so it
 * is never "dead" for anything that acts on death -- the same carve-out the backend's
 * ``is_lifecycle_dead`` makes. */
export function isAgentProcessDead(state: string): boolean {
  return !ALIVE_STATES.has(state) && state !== "UNKNOWN";
}
