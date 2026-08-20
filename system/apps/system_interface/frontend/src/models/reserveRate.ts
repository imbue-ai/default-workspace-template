/**
 * The per-event rate unloaded history is priced at, and the rule for when to
 * re-learn it.
 *
 * `RowGeometryIndex` reserves real measured heights for the ranges it covers and
 * asks its caller for a rate to price the rest. This is where that rate comes
 * from.
 *
 * Deliberately not read off the geometry index, which only admits rows that have
 * *settled* -- half a second after paint, and only folded in on whatever redraw
 * happens next. Pricing the reserve off that meant it sat at the cold default
 * until the first settle landed and then collapsed several-fold in one step, and
 * that step routinely fell on the user's own scroll: the shorter content clamped
 * scrollTop, which re-armed tail-following underneath them. Taking the rate from
 * every row that has been measured at all converges it within a frame of first
 * paint, while the viewport is still pinned to the tail and the change is
 * invisible.
 *
 * Two rules keep the reserved height from churning once it is roughly right:
 *
 * **A relative threshold.** Once converged, ordinary measurement noise leaves the
 * rate alone, so the scroll height stays put and the scrollbar does not crawl. A
 * genuine change -- the cold default meeting reality, or a conversation whose
 * character shifts -- clears it easily.
 *
 * **A settle point.** The reserve prices history this client has never seen, so
 * being approximately right and STABLE beats being precise and moving: every
 * later refinement would resize the scroll container under a reader, and a shrink
 * that pulls the bottom up to the viewport re-arms tail-following underneath
 * them.
 *
 * Kept DOM-free so the arithmetic is unit-testable on its own; the caller decides
 * when it is worth asking (a hidden panel has nothing laid out, so it can learn
 * nothing).
 */

import { DEFAULT_EVENT_HEIGHT_PX } from "./rowGeometry";

/**
 * How far the observed rate must move, relative to the rate in use, before the
 * reserve adopts it.
 */
export const RESERVE_RATE_CHANGE_THRESHOLD = 0.1;

/**
 * How many measured rows are enough to fix the rate for good. Twenty rows is a
 * representative sample of a transcript's mix, and is reached within the first
 * screens.
 */
export const RESERVE_RATE_SAMPLE_ROWS = 20;

/**
 * What one rendered row says about the rate.
 *
 * A row that has not been measured yet is still evidence, at its estimate,
 * rather than left out. Taking the rate over only the measured rows made it
 * lurch as different kinds of row settled at different times -- short user
 * bubbles first, taller assistant rows after -- which moved the reserve by
 * thousands of pixels seconds after load. Including every row keeps the mix
 * representative from the first frame, so the rate only tightens as estimates
 * are replaced by measurements rather than swinging.
 */
export interface ReserveRateSample {
  /** Events the row covers. A row covering none says nothing about a rate per
   *  event, and is ignored. */
  readonly events: number;
  /** The row's height: its measurement when it has one, else its estimate. */
  readonly height: number;
  /** Whether `height` is a measurement rather than an estimate. */
  readonly is_measured: boolean;
}

/** Pixels per event, and whether it has stopped moving. */
export interface ReserveRate {
  readonly rate: number;
  readonly is_settled: boolean;
}

/**
 * Where a transcript starts, before anything about it has been rendered. Shared
 * rather than constructed per caller, which is safe because every field is
 * read-only and `nextReserveRate` always returns a fresh value.
 */
export const COLD_RESERVE_RATE: ReserveRate = { rate: DEFAULT_EVENT_HEIGHT_PX, is_settled: false };

/**
 * The rate to use next, given what the loaded window currently looks like.
 *
 * `current` comes back untouched once it has settled, and whenever the window
 * offers no evidence -- a rate divided out of nothing measures nothing, and a
 * window like that must not settle the answer either.
 */
export function nextReserveRate(samples: readonly ReserveRateSample[], current: ReserveRate): ReserveRate {
  if (current.is_settled) {
    return current;
  }
  let totalHeight = 0;
  let totalEvents = 0;
  let measuredRows = 0;
  for (const sample of samples) {
    if (sample.events <= 0) {
      continue;
    }
    if (sample.is_measured) {
      measuredRows += 1;
    }
    totalHeight += sample.height;
    totalEvents += sample.events;
  }
  if (totalEvents <= 0 || totalHeight <= 0) {
    return current;
  }
  const observed = totalHeight / totalEvents;
  const hasMoved = Math.abs(observed - current.rate) / current.rate > RESERVE_RATE_CHANGE_THRESHOLD;
  return {
    rate: hasMoved ? observed : current.rate,
    is_settled: measuredRows >= RESERVE_RATE_SAMPLE_ROWS,
  };
}
