/**
 * The words and numbers the memory tab shows: sizes, idle times, harness names, a chat's state, and the
 * headline for each memory status. Pure, so each is tested on its own.
 */

import type { ActivityItem, MemoryStatus, MemorySummary } from "../models/summary";

const BYTES_PER_KIB = 1024;
const BYTES_PER_MIB = 1024 * 1024;
const BYTES_PER_GIB = 1024 * 1024 * 1024;
// A chat counts as idle enough to suggest stopping once its last message is this old: the same rule as the
// workspace's own memory-candidates list (system/services/oom_priority/bin/memory_candidates.py, IDLE_AFTER_SECONDS).
export const IDLE_SUGGESTION_SECONDS = 15 * 60;

export function formatBytes(bytes: number): string {
  // Compared after rounding, so 1023.7 MB reads as "1.0 GB" rather than "1024 MB".
  if (Math.round(bytes / BYTES_PER_MIB) >= 1024) return `${(bytes / BYTES_PER_GIB).toFixed(1)} GB`;
  if (bytes > 0 && bytes < BYTES_PER_MIB) return "<1 MB";
  return `${Math.round(bytes / BYTES_PER_MIB)} MB`;
}

export function formatKib(kib: number): string {
  return formatBytes(kib * BYTES_PER_KIB);
}

/** A part's share of a whole as people say it: "7%", and "<1%" for a sliver that would round to nothing. */
export function formatShare(partKib: number, wholeKib: number): string {
  if (wholeKib <= 0 || partKib <= 0) return "0%";
  const percent = (100 * partKib) / wholeKib;
  return percent < 1 ? "<1%" : `${Math.round(percent)}%`;
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return "just now";
  // Each unit is used only while its rounded count stays below the next unit, so 59.5 minutes reads "1 hr", not "60 min".
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.round(seconds / 3600);
  if (hours < 24) return `${hours} hr`;
  const days = Math.round(seconds / 86400);
  return days === 1 ? "1 day" : `${days} days`;
}

const HARNESS_NAMES: Record<string, string> = {
  claude: "Claude",
  codex: "Codex",
  "pi-coding": "Pi",
  opencode: "OpenCode",
  antigravity: "Antigravity",
};

export function harnessName(harness: string | null): string | null {
  if (harness === null) return null;
  return HARNESS_NAMES[harness] ?? harness;
}

export function idleSeconds(item: ActivityItem, nowMs: number): number | null {
  return item.last_messaged_at === null ? null : Math.max(0, nowMs / 1000 - item.last_messaged_at);
}

/** One plain line on what a chat is doing, from the chat app's status. */
export function chatStateLine(item: ActivityItem, nowMs: number): string {
  const idle = idleSeconds(item, nowMs);
  switch (item.state) {
    case "working":
      return "Working right now";
    case "attention":
      return "Needs you";
    case "idle":
      if (idle === null) return "Waiting for you";
      return idle < 60
        ? "Waiting for you · last message just now"
        : `Waiting for you · last message ${formatDuration(idle)} ago`;
    case "stopped":
      return "Stopped · starts again when you send it a message";
    case "running":
      return item.description === "" ? "Running" : `Running · ${item.description}`;
    case "error":
      return "Something went wrong in this chat";
    default:
      return item.state;
  }
}

/** Whether a chat is running but idle long enough that stopping it is worth suggesting. */
export function isStopSuggested(item: ActivityItem, nowMs: number): boolean {
  if (item.kind !== "CHAT" || item.state !== "idle" || item.rss_kib === 0) return false;
  const idle = idleSeconds(item, nowMs);
  return idle !== null && idle >= IDLE_SUGGESTION_SECONDS;
}

/** One plain line on an app or service's state. */
export function programStateLine(item: ActivityItem): string {
  if (item.state === "UNKNOWN") return `${item.description} · state unknown right now`;
  if (item.state === "STARTING") return "Starting…";
  if (item.state !== "RUNNING") {
    // Only a cleanly stopped app comes back on its next request; one that crashed (FATAL, BACKOFF) says so.
    const isCleanlyStopped = item.state === "STOPPED" || item.state === "EXITED";
    return item.is_restarted_on_open && isCleanlyStopped
      ? "Not running · starts when you open it"
      : `Not running (${item.state.toLowerCase()})`;
  }
  if (item.kind === "APP" && item.is_on_demand) return "Stops by itself a minute after its last window closes";
  if (item.kind === "APP" && item.always_on_reason !== null) return `${item.description} · ${item.always_on_reason}`;
  return item.description;
}

export interface Headline {
  readonly tone: "success" | "warning" | "danger";
  readonly badge: string;
  readonly title: string;
  readonly body: string;
}

/** Who closes something when memory runs out, and when, in plain words. */
export function closingSentence(memory: MemorySummary): string {
  switch (memory.closer) {
    case "MEMORY_GUARD":
      return (
        `When less than ${memory.min_available_percent ?? 10}% is free, the workspace's memory guard closes ` +
        "something on its own, most likely the one marked below."
      );
    case "SYSTEM_LIMIT":
      return "When memory is completely full, the system closes something on its own, most likely the one marked below.";
  }
}

export function headlineFor(memory: MemorySummary): Headline {
  const amounts = `Using ${formatBytes(memory.used_bytes)} of ${formatBytes(memory.limit_bytes)}.`;
  const status: MemoryStatus = memory.status;
  switch (status) {
    case "COMFORTABLE":
      return { tone: "success", badge: "Comfortable", title: "Your workspace has room to spare.", body: amounts };
    case "TIGHT":
      return {
        tone: "warning",
        badge: "Getting tight",
        title: "Memory is getting tight.",
        body: `${amounts} If it fills up, the workspace closes things to make room.`,
      };
    case "CRITICAL":
      return {
        tone: "danger",
        badge: "Almost full",
        title: "Your workspace is about to start closing things.",
        body: `${amounts} ${closingSentence(memory)} Free some memory now to choose for yourself.`,
      };
  }
}
