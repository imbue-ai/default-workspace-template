// Pure decision for surfacing a highlighted agent's tab (e.g. the nightly
// Caretaker). Kept DOM-free and dependency-free so it can be unit-tested in
// isolation; DockviewWorkspace imports it and supplies the live inputs.

export type HighlightSurfaceDecision = "open" | "flash" | "noop";

/**
 * Decide what to do with a highlighted agent on a given agents_updated snapshot.
 *
 * The decision is derived SOLELY from the persisted acknowledgement (the last
 * highlight key the user viewed) versus the agent's current key -- never from
 * any in-session/in-memory "already surfaced this key" bookkeeping. That is what
 * makes it idempotent across a WebSocket reconnect: a run that appeared while the
 * UI was disconnected (e.g. the Caretaker firing at 3 AM while the laptop slept)
 * still surfaces on the first snapshot after reconnect, and keeps surfacing until
 * the user actually views (or dismisses) it.
 *
 * - ``"open"``  -- closed tab with an unacknowledged run: open it (in the
 *   background) so it flashes.
 * - ``"flash"`` -- already-open background tab with an unacknowledged run:
 *   (re-)arm its flash. Idempotent: re-arming an already-flashing tab is a no-op,
 *   so this can run on every snapshot without a flash storm.
 * - ``"noop"``  -- not highlighted, or the current run is already acknowledged.
 */
export function decideHighlightSurface(input: {
  isHighlighted: boolean;
  currentKey: string;
  acknowledgedKey: string | undefined;
  isTabOpen: boolean;
}): HighlightSurfaceDecision {
  if (!input.isHighlighted) return "noop";
  if (input.acknowledgedKey === input.currentKey) return "noop";
  return input.isTabOpen ? "flash" : "open";
}
