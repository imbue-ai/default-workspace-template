/**
 * Whether an agent's mngr lifecycle state says its process is dead, for what acts on death
 * (unmounting a chat's terminal frame).
 *
 * In this all-local deployment every non-running state is recoverable -- DONE (claude exited
 * to a shell), STOPPED (the tmux window is gone), REPLACED -- because sending the agent a
 * message revives it; dead here means only that the process is not up now.
 */

// Lifecycle states in which the agent process is alive, whether working (RUNNING) or idle
// (WAITING). RUNNING_UNKNOWN_AGENT_TYPE is an agent running under a process name mngr can't
// confirm; it counts as running.
const ALIVE_STATES: ReadonlySet<string> = new Set(["RUNNING", "RUNNING_UNKNOWN_AGENT_TYPE", "WAITING"]);

/** True iff ``state`` POSITIVELY says the agent process is dead.

 * UNKNOWN means the observer could not look (non-evidence, the agent may be running fine), so
 * it is never "dead" for anything that acts on death -- the same carve-out the backend's
 * ``is_lifecycle_dead`` makes. */
export function isAgentProcessDead(state: string): boolean {
  return !ALIVE_STATES.has(state) && state !== "UNKNOWN";
}
