/**
 * An estimate of how much of a subscription's limit each call used, from its tokens.
 *
 * A harness reports only each limit window's share used, for the whole account: it counts the account's use
 * everywhere it is signed in, not only this workspace's. So the reported share is not spread over this
 * workspace's calls. What they added is measured instead: the rise in the window's share from its first reading
 * here (the lowest the collector saw in it) to its latest, divided by the list price of this workspace's
 * subscription calls between the two (the collector's API-rate price, which weighs an Opus token above a Haiku
 * one much as the limits do), gives a share per dollar, and a call's estimated share is its list price times that.
 * It is an estimate: the providers do not publish how their limits are counted, the shares come in whole percents,
 * and use elsewhere between the two readings still lands in the rise.
 *
 * Once a window resets its harness reports nothing for it until the next call, so the last reading's rate is kept:
 * it is the account's rate, which a reset does not change, and the page says the estimate rests on it.
 */

import type { Group, TotalsRow } from "./api";
import { type BreakdownId, NO_SOURCE_NAMES, breakdownLines } from "./breakdown";
import type { LimitWindow } from "./providers";

/** The share of one limit window a dollar of list price uses, for one subscription. */
export interface UsageScale {
  windowKey: string;
  label: string;
  percentPerDollar: number;
  /** Whether the window has reset since the reading the rate comes from. */
  isFromEarlierWindow: boolean;
  /** When the reading's window started and reset, for marking on a chart. */
  windowStart: Date;
  resetsAt: Date;
}

/**
 * How far past a reading a span's edge sits. A reading that rides on a call is dated to that call's end and
 * already counts it, and the collector keeps calls that ended in ``[since, until)`` (dated to the microsecond,
 * which a ``Date`` truncates to the millisecond), so an edge just after a reading puts its call on the reading's
 * side: out of a span that starts there, in a span that ends there.
 */
const READING_INCLUDES_MS = 1;

/** The calls a window's rise is measured over, and how much it rose across them. */
export interface CalibrationSpan {
  since: Date;
  until: Date;
  /** The rise in share from the window's first reading to its latest, in percentage points. */
  rise: number;
}

/**
 * The span from just after a window's first reading to just after its latest, whether or not it has reset since,
 * and the share the window rose by across it; null while it has not risen, since there is nothing to measure yet.
 */
export function calibrationSpan(window: LimitWindow): CalibrationSpan | null {
  if (window.readingPercent === null || window.firstPercent === null || window.firstObservedAt === null) return null;
  const rise = window.readingPercent - window.firstPercent;
  if (rise <= 0 || window.firstObservedAt >= window.observedAt) return null;
  return {
    since: new Date(window.firstObservedAt.getTime() + READING_INCLUDES_MS),
    until: new Date(window.observedAt.getTime() + READING_INCLUDES_MS),
    rise,
  };
}

/** When the window of a limit's last reading started, or null for a window with no length or reset time. */
export function limitWindowStart(window: LimitWindow): Date | null {
  if (window.readingResetsAt === null || window.windowSeconds === null) return null;
  return new Date(window.readingResetsAt.getTime() - window.windowSeconds * 1000);
}

/** The share per dollar of list price, or null when the span's calls had no list price to spread it over. */
export function percentPerDollar(rise: number, listPrice: number): number | null {
  return listPrice > 0 ? rise / listPrice : null;
}

/** The scale a subscription's usage is shown against: the window its viewer picked, else its shortest. */
export function chosenScale(scales: readonly UsageScale[], windowKey: string | undefined): UsageScale | undefined {
  return scales.find((scale) => scale.windowKey === windowKey) ?? scales[0];
}

/** "0.42%", "3.1%", "<0.01%": a call's estimated share of a limit. */
export function formatShare(percent: number): string {
  if (percent > 0 && percent < 0.01) return "<0.01%";
  if (percent < 1) return `${percent.toFixed(2)}%`;
  return `${percent.toFixed(1)}%`;
}

/** The key a breakdown line is known by, across answers for different spans of time. */
export function lineKey(group: Group): string {
  return JSON.stringify(group);
}

/**
 * Each breakdown line's estimated share of a limit within the limit's most recent window, by ``lineKey``: its list
 * price there times the scale's rate. A line with no subscription calls in that window is left out.
 */
export function limitShares(
  breakdown: BreakdownId,
  windowRows: readonly TotalsRow[],
  scale: UsageScale,
): Map<string, number> {
  const shares = new Map<string, number>();
  for (const line of breakdownLines(breakdown, windowRows, [], NO_SOURCE_NAMES)) {
    if (line.listPrice !== null) shares.set(lineKey(line.group), line.listPrice * scale.percentPerDollar);
  }
  return shares;
}
