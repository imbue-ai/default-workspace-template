/**
 * Virtual end-spacer sizing: unloaded history is reserved at the measured
 * physical average px/event, smoothed so each measurement nudges rather than
 * jolts. Every update reports the top-spacer height change as
 * `scrollTopDeltaPx` -- the exact compensation the engine must apply in the same
 * frame to keep content stationary (the bottom spacer needs none). The engine
 * applies updates only in the ELSEWHERE scrollbar state; while the user holds
 * the scrollbar, spacers stay frozen along with the mapping.
 */

export const DEFAULT_SPACER_PX_PER_EVENT = 160;
export const SPACER_SMOOTHING_ALPHA = 0.2;

export interface SpacerUpdateInput {
  readonly previousEstimatePxPerEvent: number;
  /** Fresh observation (physical height / loaded events), or null to keep the estimate. */
  readonly observedPxPerEvent: number | null;
  readonly smoothingAlpha: number;
  readonly olderUnloadedCount: number;
  readonly newerUnloadedCount: number;
  readonly previousSpacerTopPx: number;
}

export interface SpacerUpdate {
  readonly estimatePxPerEvent: number;
  readonly spacerTopPx: number;
  readonly spacerBottomPx: number;
  /** Apply to scrollTop in the same frame the new spacer heights render. */
  readonly scrollTopDeltaPx: number;
}

export function computeObservedPxPerEvent(physicalContentHeightPx: number, loadedEventCount: number): number | null {
  if (loadedEventCount <= 0 || physicalContentHeightPx <= 0) {
    return null;
  }
  return physicalContentHeightPx / loadedEventCount;
}

export function computeSpacerUpdate(input: SpacerUpdateInput): SpacerUpdate {
  const estimatePxPerEvent =
    input.observedPxPerEvent === null
      ? input.previousEstimatePxPerEvent
      : input.previousEstimatePxPerEvent +
        input.smoothingAlpha * (input.observedPxPerEvent - input.previousEstimatePxPerEvent);
  const spacerTopPx = Math.round(Math.max(0, input.olderUnloadedCount) * estimatePxPerEvent);
  const spacerBottomPx = Math.round(Math.max(0, input.newerUnloadedCount) * estimatePxPerEvent);
  return {
    estimatePxPerEvent,
    spacerTopPx,
    spacerBottomPx,
    scrollTopDeltaPx: spacerTopPx - input.previousSpacerTopPx,
  };
}
