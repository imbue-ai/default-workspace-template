/**
 * How the views say what the API answers: sizes, shares, times, the breakdown's tags and rows, the memory
 * view's advice, and the backups' summary lines.
 */

import type { BackupSummary, StatsResponse, StopKind, UsageRow } from "./api";

/** Above this share of memory in use, the memory guard stops programs (the mark on the bar, the graph's line). */
export const GUARD_PERCENT = 90;
/** From this share of memory in use, the bar and the note warn. */
export const MEMORY_WARN_PERCENT = 80;

export function size(bytes: number): string {
  const gb = bytes / 2 ** 30;
  if (gb >= 10) return `${gb.toFixed(0)} GB`;
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  return `${Math.round(bytes / 2 ** 20)} MB`;
}

export function pct(part: number, whole: number): number {
  return whole ? Math.round((part / whole) * 100) : 0;
}

/** A share of the CPU as a percentage, or null for a row with none (shown as a dash). */
export function sharePct(share: number | null | undefined): string | null {
  if (share === null || share === undefined) return null;
  return `${(share * 100).toFixed(share < 0.1 ? 1 : 0)}%`;
}

export function plural(count: number, one: string, many: string = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** Descriptions arrive as sentence fragments ("a code checker"); shown on their own line, they start capitalized. */
export function capitalized(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

export function clock(at: Date): string {
  return at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** A time of the last 24 hours: the clock time, and "yesterday" when it is not today. */
export function when(iso: string, now: Date = new Date()): string {
  const at = new Date(iso);
  return clock(at) + (at.toDateString() === now.toDateString() ? "" : " yesterday");
}

export function ago(seconds: number): string {
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

/** The small grey text beside a breakdown row's heading. */
export function usageTag(row: UsageRow): string {
  if (row.id === "scratch") return `of ${size(row.limit ?? 0)} limit`;
  if (row.hint) return row.hint;
  if (!["chats", "checkers", "apps"].includes(row.id)) return "";
  return row.items.length ? `${row.items.length} running` : "none running";
}

/** A row that lists nothing and holds nothing says nothing worth a line: Code checkers and Your apps are left out
 * until something runs under them. */
export function shownRows(usage: readonly UsageRow[]): UsageRow[] {
  return usage.filter((row) => !(["checkers", "apps"].includes(row.id) && !row.items.length && !row.bytes));
}

/** The CPU view's rows: by share, and their items by share too; without a reading, the rows that have processes
 * keep their order (and show dashes). */
export function cpuRows(stats: StatsResponse): UsageRow[] {
  if (stats.cpu.sampled_at === null) {
    return shownRows(stats.usage).filter((row) => ["chats", "checkers", "workspace", "apps"].includes(row.id));
  }
  return shownRows(stats.usage)
    .filter((row) => row.cpu !== null && row.cpu !== undefined)
    .sort((a, b) => (b.cpu ?? 0) - (a.cpu ?? 0))
    .map((row) => ({ ...row, items: [...row.items].sort((a, b) => (b.cpu || 0) - (a.cpu || 0)) }));
}

/** What each kind of Stop button is called, says while it works, and asks (of the item it names) before it acts. */
export const STOP_WORDS: Record<
  StopKind,
  { verb: string; busy: string; ask: (name: string) => string; then: string }
> = {
  chat: { verb: "Stop", busy: "Stopping…", ask: (name) => `Stop ${name}?`, then: "Its next message starts it again." },
  checkers: {
    verb: "Stop",
    busy: "Stopping…",
    ask: (name) => `Stop the code checkers of ${name}?`,
    then: "The chat starts them again when it needs them.",
  },
  app: { verb: "Quit", busy: "Quitting…", ask: (name) => `Quit ${name}?`, then: "Opening it starts it again." },
};

const ADVICE_MIN_BYTES = 100 * 2 ** 20;

/** What the user can do about the largest kind of memory they can free (the memory view's note), or "". */
export function freeingAdvice(usage: readonly UsageRow[]): string {
  const freeable = usage.filter((row) => ["chats", "apps", "checkers", "scratch"].includes(row.id));
  if (!freeable.length) return "";
  const top = freeable.reduce((a, b) => (b.bytes > a.bytes ? b : a));
  if (top.bytes < ADVICE_MIN_BYTES) return "";
  return (
    {
      chats: "Chats are using the most memory. Stopping chats you aren't using frees memory.",
      apps: "Your apps are using the most memory. Quitting apps you aren't using frees memory.",
      checkers:
        "Code checkers are using the most memory. Asking an agent to stop its code checkers, or stopping its chat, frees memory.",
      scratch: `Temporary files are using ${size(top.bytes)}. They're cleared when the workspace restarts.`,
    }[top.id] ?? ""
  );
}

/** The memory view's note, or null when memory is fine and nothing was stopped: how full memory is and what happens
 * when it fills, what the guard already stopped, and, when memory is short, which memory the user can free. */
export function memoryNote(stats: StatsResponse): { head: string; rest: string } | null {
  const percent = pct(stats.memory.used, stats.memory.total);
  const isShort = percent >= MEMORY_WARN_PERCENT;
  const stopped = stats.stops.length;
  if (!isShort && !stopped) return null;
  const parts = [
    isShort ? `Above ${GUARD_PERCENT}%, the workspace stops programs to free memory.` : "",
    stopped ? `The workspace stopped ${plural(stopped, "program")} in the last 24 hours.` : "",
    isShort ? freeingAdvice(stats.usage) : "",
  ];
  return { head: `Memory is ${percent}% full.`, rest: parts.filter((part) => part !== "").join(" ") };
}

/** How a backup's state reads: the dot's tone, the bold line, and the line after it. */
export type BackupTone = "none" | "live" | "warn" | "bad";

export function backupLines(backups: BackupSummary, nowMs: number = Date.now()): [BackupTone, string, string] {
  const since = (iso: string): string => ago((nowMs - Date.parse(iso)) / 1000);
  const lastSuccess = backups.last_success_at;
  switch (backups.state) {
    case "not_configured":
      return [
        "none",
        "Backups aren't set up for this workspace.",
        "Nothing is backed up until a repository is configured.",
      ];
    case "running":
      return [
        "live",
        "Backing up now.",
        lastSuccess ? `The last backup completed ${since(lastSuccess)}.` : "This is the first backup.",
      ];
    case "up_to_date":
      return ["live", lastSuccess ? `Backed up ${since(lastSuccess)}.` : "Backed up.", "Up to date."];
    case "stale":
      return [
        "warn",
        lastSuccess ? `Last backed up ${since(lastSuccess)}.` : "Not backed up yet.",
        "A backup is overdue.",
      ];
    case "failing":
      return [
        "bad",
        `The last backup failed${backups.last_failure_at ? ` ${since(backups.last_failure_at)}` : ""}.`,
        lastSuccess ? `The last one that completed was ${since(lastSuccess)}.` : "None has completed yet.",
      ];
    case "out_of_storage":
      return [
        "bad",
        "Out of backup storage.",
        lastSuccess
          ? `Backups are paused until space is freed; the last one that completed was ${since(lastSuccess)}.`
          : "Backups are paused until space is freed.",
      ];
  }
}

/** How often backups run, and how many failed today: "The whole workspace is backed up every 6 hours, ...". */
export function backupSchedule(backups: BackupSummary): string {
  const seconds = backups.interval_seconds;
  const minutes = Math.round(seconds / 60);
  const every =
    seconds >= 3600
      ? `every ${Math.round(seconds / 3600)} hour${seconds >= 7200 ? "s" : ""}`
      : `every ${minutes} minute${minutes === 1 ? "" : "s"}`;
  const failures = backups.failures_last_day
    ? ` ${backups.failures_last_day} backup${backups.failures_last_day > 1 ? "s" : ""} failed in the last 24 hours.`
    : "";
  return `The whole workspace is backed up ${every}, encrypted.${failures}`;
}
