// Pure decision for surfacing a highlighted agent's tab (e.g. the weekly
// Caretaker). Kept DOM-free and dependency-free so it can be unit-tested in
// isolation; DockviewWorkspace imports it and supplies the live inputs.

export type HighlightSurfaceDecision = "open" | "noop";

/**
 * Decide what to do with a highlighted agent on a given agents_updated snapshot.
 *
 * The decision is derived SOLELY from the persisted acknowledgement (the last
 * highlight key the user viewed) versus the agent's current key -- never from
 * any in-session/in-memory "already surfaced this key" bookkeeping. That is what
 * makes it idempotent across a WebSocket reconnect: a run that appeared while the
 * UI was disconnected (e.g. the Caretaker firing overnight while the laptop
 * slept) still surfaces on the first snapshot after reconnect.
 *
 * - ``"open"`` -- closed tab with an unacknowledged run: open it (in the
 *   background, without stealing focus).
 * - ``"noop"`` -- not highlighted, the current run is already acknowledged, or
 *   the tab is already open (nothing to do; viewing it acknowledges the run).
 */
export function decideHighlightSurface(input: {
  isHighlighted: boolean;
  currentKey: string;
  acknowledgedKey: string | undefined;
  isTabOpen: boolean;
}): HighlightSurfaceDecision {
  if (!input.isHighlighted) return "noop";
  if (input.acknowledgedKey === input.currentKey) return "noop";
  return input.isTabOpen ? "noop" : "open";
}
