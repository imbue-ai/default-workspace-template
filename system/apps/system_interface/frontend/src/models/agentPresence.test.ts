import { describe, expect, it } from "vitest";

import { AgentPresenceTracker, CONFIRM_GONE_SNAPSHOTS } from "./agentPresence";

/** Feed `count` snapshots that all agree the agent is missing. */
function noteAbsentSnapshots(tracker: AgentPresenceTracker, count: number): void {
  for (let i = 0; i < count; i++) {
    tracker.noteSnapshot(false);
  }
}

describe("AgentPresenceTracker", () => {
  it("does not confirm on absence alone, however long it lasts", () => {
    const tracker = new AgentPresenceTracker();
    noteAbsentSnapshots(tracker, CONFIRM_GONE_SNAPSHOTS + 10);
    // The transcript loaded fine, so this is an agent the agent list is simply
    // not reporting -- a filtered or not-yet-discovered agent, not a dead one.
    expect(tracker.isConfirmedGone).toBe(false);
  });

  it("does not confirm on a 404 alone", () => {
    const tracker = new AgentPresenceTracker();
    tracker.noteTranscriptMissing(true);
    expect(tracker.isConfirmedGone).toBe(false);
  });

  it("does not confirm before the absence outlives the debounce", () => {
    const tracker = new AgentPresenceTracker();
    tracker.noteTranscriptMissing(true);
    noteAbsentSnapshots(tracker, CONFIRM_GONE_SNAPSHOTS - 1);
    expect(tracker.isConfirmedGone).toBe(false);
  });

  it("confirms once both signals agree across consecutive snapshots", () => {
    const tracker = new AgentPresenceTracker();
    tracker.noteTranscriptMissing(true);
    noteAbsentSnapshots(tracker, CONFIRM_GONE_SNAPSHOTS);
    expect(tracker.isConfirmedGone).toBe(true);
  });

  // The scenario the debounce exists for: the observe pipeline restarts, so one
  // snapshot lists nothing and the transcript fetch 404s at the same moment. The
  // agent then comes back. It must never have been declared destroyed.
  it("never confirms when a single degenerate snapshot is followed by a recovery", () => {
    const tracker = new AgentPresenceTracker();
    tracker.noteTranscriptMissing(true);
    tracker.noteSnapshot(false);
    expect(tracker.isConfirmedGone).toBe(false);
    tracker.noteSnapshot(true);
    expect(tracker.isConfirmedGone).toBe(false);
    // ...and the run of absences restarted from scratch, so a later single
    // absent snapshot does not tip it over either.
    tracker.noteSnapshot(false);
    expect(tracker.isConfirmedGone).toBe(false);
  });

  it("un-confirms when the transcript loads after all", () => {
    const tracker = new AgentPresenceTracker();
    tracker.noteTranscriptMissing(true);
    noteAbsentSnapshots(tracker, CONFIRM_GONE_SNAPSHOTS);
    expect(tracker.isConfirmedGone).toBe(true);

    tracker.noteTranscriptMissing(false);
    expect(tracker.isConfirmedGone).toBe(false);
  });

  it("forgets both signals on reset", () => {
    const tracker = new AgentPresenceTracker();
    tracker.noteTranscriptMissing(true);
    noteAbsentSnapshots(tracker, CONFIRM_GONE_SNAPSHOTS);
    tracker.reset();
    expect(tracker.isConfirmedGone).toBe(false);

    // A single absent snapshot after the reset must not immediately re-confirm.
    tracker.noteTranscriptMissing(true);
    tracker.noteSnapshot(false);
    expect(tracker.isConfirmedGone).toBe(false);
  });
});
