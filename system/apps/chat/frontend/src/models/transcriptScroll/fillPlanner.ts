/**
 * Progressive-fill planning for the physical layer: given where the user is
 * (the fill focus) and what is loaded, decide the single next action -- a
 * bounded fetch, an eviction, or nothing. The engine executes one action at a
 * time (single-flight) and re-plans when it lands, so the physical window
 * converges on up to `capEvents` events centered on the user.
 *
 * The ladder: an instant tail page on first load, jump landings load a medium
 * window around the target, and everything else grows in large chunks.
 */

import type { EventIndex, PhysicalExtent } from "./types";

export const PHYSICAL_CAP_EVENTS = 50_000;
export const FILL_CHUNK_LIMIT = 2_000;
export const INITIAL_TAIL_LIMIT = 50;
export const JUMP_WINDOW_LIMIT = 500;

export type FillFocus = { readonly kind: "tail" } | { readonly kind: "index"; readonly index: EventIndex };

export type FillAction =
  | { readonly kind: "fetch-tail"; readonly limit: number }
  | { readonly kind: "fetch-before"; readonly limit: number } // page older than the loaded window
  | { readonly kind: "fetch-after"; readonly limit: number } // page newer than the loaded window
  | { readonly kind: "fetch-at-offset"; readonly offset: EventIndex; readonly limit: number } // window replace
  | { readonly kind: "evict"; readonly side: "older" | "newer"; readonly count: number }
  | { readonly kind: "idle" };

export interface FillPlanInput {
  /** The loaded window, or null before any load has completed. */
  readonly physical: PhysicalExtent | null;
  /** The server's total event count, or null before any load has completed. */
  readonly totalEvents: number | null;
  readonly focus: FillFocus;
  readonly capEvents: number;
  readonly chunkLimit: number;
  readonly initialTailLimit: number;
  readonly jumpWindowLimit: number;
}

function clampIndex(index: number, totalEvents: number): EventIndex {
  return Math.min(Math.max(0, index), Math.max(0, totalEvents - 1));
}

function windowReplaceAround(focusIndex: EventIndex, totalEvents: number, jumpWindowLimit: number): FillAction {
  const maxOffset = Math.max(0, totalEvents - jumpWindowLimit);
  const offset = Math.min(Math.max(0, focusIndex - Math.floor(jumpWindowLimit / 2)), maxOffset);
  return { kind: "fetch-at-offset", offset, limit: jumpWindowLimit };
}

export function planNextFill(input: FillPlanInput): FillAction {
  const { physical, totalEvents, focus, capEvents, chunkLimit, initialTailLimit, jumpWindowLimit } = input;

  // Nothing known yet: the instant first paint is the tail page.
  if (totalEvents === null) {
    return { kind: "fetch-tail", limit: initialTailLimit };
  }
  if (totalEvents <= 0) {
    return { kind: "idle" };
  }

  const focusIndex = focus.kind === "tail" ? totalEvents - 1 : clampIndex(focus.index, totalEvents);
  const loadedCount = physical === null ? 0 : Math.max(0, physical.endIndex - physical.firstIndex);

  // Nothing loaded (an empty window, e.g. after a reset): land a window on the focus.
  if (physical === null || loadedCount === 0) {
    if (focus.kind === "tail") {
      return { kind: "fetch-tail", limit: initialTailLimit };
    }
    return windowReplaceAround(focusIndex, totalEvents, jumpWindowLimit);
  }

  // Focus outside the loaded window: extend toward it when near, replace the
  // window in one bounded read when far (a deep fling or a restore).
  if (focusIndex < physical.firstIndex) {
    const gap = physical.firstIndex - focusIndex;
    if (gap > chunkLimit) {
      return windowReplaceAround(focusIndex, totalEvents, jumpWindowLimit);
    }
    return { kind: "fetch-before", limit: chunkLimit };
  }
  if (focusIndex >= physical.endIndex) {
    const gap = focusIndex - (physical.endIndex - 1);
    if (gap > chunkLimit) {
      return windowReplaceAround(focusIndex, totalEvents, jumpWindowLimit);
    }
    return { kind: "fetch-after", limit: chunkLimit };
  }

  // Focus inside the window: converge on cap-sized coverage centered on it.
  const halfCap = Math.floor(capEvents / 2);
  let desiredStart = focusIndex - halfCap;
  let desiredEnd = desiredStart + capEvents;
  if (desiredStart < 0) {
    desiredEnd = Math.min(totalEvents, desiredEnd - desiredStart);
    desiredStart = 0;
  }
  if (desiredEnd > totalEvents) {
    desiredStart = Math.max(0, desiredStart - (desiredEnd - totalEvents));
    desiredEnd = totalEvents;
  }
  const beforeDeficit = Math.max(0, physical.firstIndex - desiredStart);
  const afterDeficit = Math.max(0, desiredEnd - physical.endIndex);

  // Over the cap: trim the side farther from the focus.
  if (loadedCount > capEvents) {
    const olderSpan = focusIndex - physical.firstIndex;
    const newerSpan = physical.endIndex - 1 - focusIndex;
    return { kind: "evict", side: olderSpan >= newerSpan ? "older" : "newer", count: loadedCount - capEvents };
  }

  const capRemaining = capEvents - loadedCount;
  if (capRemaining <= 0) {
    // Full but lopsided: make room on the surplus side once the deficit toward
    // the focus exceeds one chunk, so steady reading near the cap doesn't churn
    // evict/fetch cycles but a real drift re-centers.
    const olderSurplus = Math.max(0, desiredStart - physical.firstIndex);
    const newerSurplus = Math.max(0, physical.endIndex - desiredEnd);
    if (beforeDeficit > chunkLimit && newerSurplus > 0) {
      return { kind: "evict", side: "newer", count: Math.min(chunkLimit, newerSurplus) };
    }
    if (afterDeficit > chunkLimit && olderSurplus > 0) {
      return { kind: "evict", side: "older", count: Math.min(chunkLimit, olderSurplus) };
    }
    return { kind: "idle" };
  }

  // Room to grow: extend toward the larger deficit.
  if (beforeDeficit >= afterDeficit && beforeDeficit > 0) {
    return { kind: "fetch-before", limit: Math.min(chunkLimit, beforeDeficit, capRemaining) };
  }
  if (afterDeficit > 0) {
    return { kind: "fetch-after", limit: Math.min(chunkLimit, afterDeficit, capRemaining) };
  }
  return { kind: "idle" };
}
