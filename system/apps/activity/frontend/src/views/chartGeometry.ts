/**
 * The memory-over-time chart's geometry, pure: scales from time and memory to the drawing's coordinates, the runs of
 * periods recording kept unbroken (a gap stays a gap, never a line drawn across it), the band of each period's lowest
 * to highest reading, the axis ticks, and the period nearest a pointer.
 */

import type { HistoryPeriod, HistoryRange } from "../models/history";

export interface ChartBox {
  readonly width: number;
  readonly height: number;
  readonly left: number;
  readonly right: number;
  readonly top: number;
  readonly bottom: number;
}

export const CHART_BOX: ChartBox = { width: 720, height: 220, left: 48, right: 16, top: 18, bottom: 28 };

export interface ChartScale {
  readonly startSeconds: number;
  readonly endSeconds: number;
  readonly maxKib: number;
}

export function xFor(box: ChartBox, scale: ChartScale, epochSeconds: number): number {
  const fraction = (epochSeconds - scale.startSeconds) / (scale.endSeconds - scale.startSeconds);
  return box.left + Math.min(1, Math.max(0, fraction)) * (box.width - box.left - box.right);
}

export function yFor(box: ChartBox, scale: ChartScale, kib: number): number {
  const fraction = scale.maxKib > 0 ? kib / scale.maxKib : 0;
  return box.top + (1 - Math.min(1, Math.max(0, fraction))) * (box.height - box.top - box.bottom);
}

/** The chart's top: the limit when known, else just above the highest reading. */
export function chartMaxKib(periods: readonly HistoryPeriod[], limitKib: number | null): number {
  if (limitKib !== null && limitKib > 0) return limitKib;
  const highest = Math.max(0, ...periods.map((period) => period.max_kib));
  return highest > 0 ? highest * 1.1 : 1;
}

/** Periods split wherever recording missed one, so a line is never drawn across a gap. */
export function unbrokenRuns(periods: readonly HistoryPeriod[], periodSeconds: number): HistoryPeriod[][] {
  const runs: HistoryPeriod[][] = [];
  for (const period of periods) {
    const run = runs[runs.length - 1];
    const previous = run === undefined ? undefined : run[run.length - 1];
    if (previous !== undefined && period.start_epoch_seconds - previous.start_epoch_seconds <= periodSeconds) {
      run.push(period);
    } else {
      runs.push([period]);
    }
  }
  return runs;
}

function middleOf(period: HistoryPeriod, periodSeconds: number): number {
  return period.start_epoch_seconds + periodSeconds / 2;
}

export function linePoints(
  box: ChartBox,
  scale: ChartScale,
  run: readonly HistoryPeriod[],
  periodSeconds: number,
): string {
  return run
    .map(
      (period) =>
        `${xFor(box, scale, middleOf(period, periodSeconds)).toFixed(1)},${yFor(box, scale, period.average_kib).toFixed(1)}`,
    )
    .join(" ");
}

export function bandPoints(
  box: ChartBox,
  scale: ChartScale,
  run: readonly HistoryPeriod[],
  periodSeconds: number,
): string {
  const top = run.map(
    (period) =>
      `${xFor(box, scale, middleOf(period, periodSeconds)).toFixed(1)},${yFor(box, scale, period.max_kib).toFixed(1)}`,
  );
  const bottom = [...run]
    .reverse()
    .map(
      (period) =>
        `${xFor(box, scale, middleOf(period, periodSeconds)).toFixed(1)},${yFor(box, scale, period.min_kib).toFixed(1)}`,
    );
  return [...top, ...bottom].join(" ");
}

/** Four evenly spaced time labels across the span, in the unit the range reads in. */
export function timeTicks(
  range: HistoryRange,
  startSeconds: number,
  endSeconds: number,
): { at: number; label: string }[] {
  const count = 4;
  return Array.from({ length: count }, (_, index) => {
    const at = startSeconds + ((endSeconds - startSeconds) * index) / (count - 1);
    return { at, label: timeLabel(range, at) };
  });
}

export function timeLabel(range: HistoryRange, epochSeconds: number): string {
  const date = new Date(epochSeconds * 1000);
  switch (range) {
    case "HOUR":
    case "DAY":
      return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    case "WEEK":
      return date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  }
}

/** The period whose middle is nearest the pointer's x, or null when there are none. */
export function nearestPeriod(
  box: ChartBox,
  scale: ChartScale,
  periods: readonly HistoryPeriod[],
  periodSeconds: number,
  pointerX: number,
): HistoryPeriod | null {
  let best: HistoryPeriod | null = null;
  let bestDistance = Infinity;
  for (const period of periods) {
    const distance = Math.abs(xFor(box, scale, middleOf(period, periodSeconds)) - pointerX);
    if (distance < bestDistance) {
      best = period;
      bestDistance = distance;
    }
  }
  return best;
}
