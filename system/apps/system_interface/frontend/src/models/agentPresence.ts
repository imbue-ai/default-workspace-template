/**
 * Decides when a chat panel's agent may be declared destroyed.
 *
 * Neither available signal is trustworthy alone. A 404 from ``/events`` means
 * the server could not resolve the agent, which covers both a genuine destroy
 * and the window where the ``mngr observe`` pipeline is restarting; and
 * ``agents_updated`` transiently drops agents for the same reason. So both are
 * required, and the absence has to survive more than one snapshot before the
 * panel commits to the tombstone -- otherwise reloading the page during an
 * observe restart would tombstone a perfectly live chat.
 *
 * One instance per chat panel; nothing here is shared or global.
 */

/** Consecutive ``agents_updated`` snapshots an agent must be missing from before
 *  its absence is believed. Two is enough to ride out a single degenerate
 *  snapshot while still settling within seconds on a workspace that is doing
 *  anything at all. */
export const CONFIRM_GONE_SNAPSHOTS = 2;

export class AgentPresenceTracker {
  #absentSnapshots = 0;
  #transcriptMissing = false;

  /** Record one ``agents_updated`` snapshot. A snapshot that lists the agent
   *  resets the run of absences -- the agent is demonstrably alive. */
  noteSnapshot(isPresent: boolean): void {
    this.#absentSnapshots = isPresent ? 0 : this.#absentSnapshots + 1;
  }

  /** Record whether the agent's transcript fetch came back 404. */
  noteTranscriptMissing(isMissing: boolean): void {
    this.#transcriptMissing = isMissing;
  }

  /** Both signals agree and the absence has outlived the debounce. */
  get isConfirmedGone(): boolean {
    return this.#transcriptMissing && this.#absentSnapshots >= CONFIRM_GONE_SNAPSHOTS;
  }

  /** Forget everything, e.g. when the panel is rebound to another agent. */
  reset(): void {
    this.#absentSnapshots = 0;
    this.#transcriptMissing = false;
  }
}
