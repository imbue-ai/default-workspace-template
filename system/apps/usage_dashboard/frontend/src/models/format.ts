/** How the page writes dollars, token counts and durations. */

/** "$4.12", "$0.0031" for a small amount (two significant digits), "$0" for none. */
export function formatDollars(amount: number): string {
  if (amount === 0) return "$0";
  if (Math.abs(amount) >= 0.01)
    return `$${amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return `$${amount.toPrecision(2)}`;
}

/** "950", "12.3K", "4.1M". */
export function formatCount(count: number): string {
  return count.toLocaleString(undefined, { notation: "compact", maximumFractionDigits: 1 });
}

/** "1 call", "12.3K calls". */
export function formatCalls(count: number): string {
  return `${formatCount(count)} ${count === 1 ? "call" : "calls"}`;
}

/** "1 turn", "1.2K turns". */
export function formatTurns(count: number): string {
  return `${formatCount(count)} ${count === 1 ? "turn" : "turns"}`;
}

/** "850 ms", "2.4 s", "1.5 min". */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${(ms / 60_000).toFixed(1)} min`;
}

export function formatPercent(part: number, whole: number): string {
  if (whole === 0) return "0%";
  const percent = (100 * part) / whole;
  return percent > 0 && percent < 1 ? "<1%" : `${Math.round(percent)}%`;
}

export function formatClock(date: Date): string {
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** A call's time: the clock for today's, the date and clock for older ones. */
export function formatCallTime(date: Date, now: Date): string {
  const isToday = date.toDateString() === now.toDateString();
  return isToday
    ? date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" })
    : date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
