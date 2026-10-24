/**
 * The page's time windows and their buckets, in the viewer's local time.
 *
 * The collector buckets days at UTC midnight, so a day view asks it for hourly buckets and adds them up into
 * local days here; a local day is not always 24 hours (a daylight-saving change makes one 23 or 25), so each
 * hour lands in the local day its start falls in.
 */

import type { TotalsRow } from "./api";

export type WindowId = "30m" | "24h" | "7d" | "month";

export interface WindowSpec {
  id: WindowId;
  label: string;
  /** What the collector is asked to bucket by. */
  bucket: "minute" | "hour";
  /** What the chart shows a bar per. */
  bars: "minute" | "hour" | "day";
  /** Whether the totals refetch on each new call, or at most every SLOW_REFRESH_MS. */
  isLive: boolean;
}

export const WINDOWS: readonly WindowSpec[] = [
  { id: "30m", label: "30 min", bucket: "minute", bars: "minute", isLive: true },
  { id: "24h", label: "24 h", bucket: "hour", bars: "hour", isLive: true },
  { id: "7d", label: "7 days", bucket: "hour", bars: "day", isLive: false },
  { id: "month", label: "This month", bucket: "hour", bars: "day", isLive: false },
];

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;

export function windowSpec(id: WindowId): WindowSpec {
  const spec = WINDOWS.find((candidate) => candidate.id === id);
  if (spec === undefined) throw new Error(`Unknown window ${id}`);
  return spec;
}

export function localMidnight(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

/** Where a window starts: a rolling 30 minutes or 24 hours, the local midnight six days back, or the 1st. */
export function windowStart(id: WindowId, now: Date): Date {
  switch (id) {
    case "30m":
      return new Date(now.getTime() - 30 * MINUTE_MS);
    case "24h":
      return new Date(now.getTime() - 24 * HOUR_MS);
    case "7d":
      return new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6);
    case "month":
      return new Date(now.getFullYear(), now.getMonth(), 1);
  }
}

/** The start of every bar in the window, oldest first, so a quiet stretch still shows as empty bars. */
export function barStarts(id: WindowId, now: Date): Date[] {
  const spec = windowSpec(id);
  const start = windowStart(id, now);
  const starts: Date[] = [];
  if (spec.bars === "day") {
    for (let day = start; day <= now; day = new Date(day.getFullYear(), day.getMonth(), day.getDate() + 1)) {
      starts.push(day);
    }
    return starts;
  }
  const step = spec.bars === "minute" ? MINUTE_MS : HOUR_MS;
  for (let time = Math.floor(start.getTime() / step) * step; time <= now.getTime(); time += step) {
    starts.push(new Date(time));
  }
  return starts;
}

/** The start of the bar a bucket that starts at ``bucketStart`` belongs to. */
export function barStartOf(id: WindowId, bucketStart: Date): Date {
  return windowSpec(id).bars === "day" ? localMidnight(bucketStart) : bucketStart;
}

/** The rows' values per bar and per series: ``seriesOf`` names each row's series, ``valueOf`` its amount. */
export function binRows(
  id: WindowId,
  now: Date,
  rows: readonly TotalsRow[],
  seriesOf: (row: TotalsRow) => string,
  valueOf: (row: TotalsRow) => number,
): { starts: Date[]; values: Map<string, number>[] } {
  const starts = barStarts(id, now);
  const indexByTime = new Map(starts.map((start, index) => [start.getTime(), index]));
  const values = starts.map(() => new Map<string, number>());
  for (const row of rows) {
    if (row.bucket_start === null) continue;
    const index = indexByTime.get(barStartOf(id, new Date(row.bucket_start)).getTime());
    if (index === undefined) continue;
    const series = seriesOf(row);
    values[index].set(series, (values[index].get(series) ?? 0) + valueOf(row));
  }
  return { starts, values };
}

export function barLabel(id: WindowId, start: Date): string {
  const spec = windowSpec(id);
  if (spec.bars === "day")
    return start.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  if (spec.bars === "hour") return start.toLocaleString(undefined, { weekday: "short", hour: "numeric" });
  return start.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
