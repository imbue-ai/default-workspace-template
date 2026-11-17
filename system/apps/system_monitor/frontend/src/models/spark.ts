/**
 * The graphs' geometry: a line (and the area under it) through the samples of a span ending at the newest one, in
 * a box ``width`` wide and ``height`` high, where y runs from 0 (full) to ``height`` (empty); and the reading
 * nearest a point in time, for the hover readout.
 */

import type { HistorySample, Span } from "./api";

export type SampleKey = "cpu" | "memory";

export const TEN_MINUTES_MS = 10 * 60 * 1000;
export const SPAN_MS: Record<Span, number> = { "10m": TEN_MINUTES_MS, "1h": 3_600_000, "24h": 24 * 3_600_000 };
/** The sampler records a reading every 5 s; readings further apart than this mean it was not running in between
 * (the machine was paused), and the line is left blank there. */
export const GAP_MS: Record<Span, number> = { "10m": 15_000, "1h": 180_000, "24h": 900_000 };

export interface SparkBox {
  width: number;
  height: number;
  spanMs: number;
  gapMs: number;
  /** When the span ends, epoch milliseconds: the newest sample's time. */
  end: number;
  /** The closed area under the line rather than the line. */
  isArea?: boolean;
}

/** Where a span drawn from ``samples`` ends: its newest sample, or now when there is none. */
export function spanEnd(samples: readonly HistorySample[], now: number = Date.now()): number {
  return samples.length ? samples[samples.length - 1].t : now;
}

/** The SVG path through the samples' ``key``; a gap longer than ``gapMs``, or an unknown reading, breaks it. */
export function sparkPath(samples: readonly HistorySample[], key: SampleKey, box: SparkBox): string {
  const { width, height, spanMs, gapMs, end, isArea = false } = box;
  const points = samples.map((sample) => ({
    t: sample.t,
    x: width - ((end - sample.t) / spanMs) * width,
    y: (1 - (sample[key] ?? 0)) * height,
    v: sample[key],
  }));
  let d = "";
  let segmentStart: number | null = null;
  points.forEach((point, i) => {
    if (point.v === null || point.v === undefined) return;
    const previous = points[i - 1];
    const isGap = i === 0 || point.t - previous.t > gapMs || previous.v === null;
    if (isGap) {
      if (isArea && segmentStart !== null) {
        d += ` L${previous.x.toFixed(1)} ${height} L${segmentStart.toFixed(1)} ${height} Z`;
      }
      segmentStart = point.x;
      d += `${isArea ? ` M${point.x.toFixed(1)} ${height} L` : " M"}${point.x.toFixed(1)} ${point.y.toFixed(1)}`;
    } else {
      d += ` L${point.x.toFixed(1)} ${point.y.toFixed(1)}`;
    }
  });
  if (isArea && segmentStart !== null && points.length) {
    d += ` L${points[points.length - 1].x.toFixed(1)} ${height} L${(segmentStart as number).toFixed(1)} ${height} Z`;
  }
  return d.trim();
}

/** The known reading nearest ``t``, or null when there is none. */
export function nearestSample(samples: readonly HistorySample[], key: SampleKey, t: number): HistorySample | null {
  let best: HistorySample | null = null;
  for (const sample of samples) {
    if (sample[key] === null) continue;
    if (best === null || Math.abs(sample.t - t) < Math.abs(best.t - t)) best = sample;
  }
  return best;
}
