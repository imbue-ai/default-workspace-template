/** Summary and history records the tests build pages and questions from. */

import type { ClosedProcess, HistoryPeriod, HistoryView } from "../models/history";
import type { ActivityItem, ActivitySummary, MemoryStatus } from "../models/summary";
import type { HistoryChartAttrs } from "../views/HistoryChart";

const GIB = 1024 * 1024 * 1024;

export function item(overrides: Partial<ActivityItem> & Pick<ActivityItem, "item_id" | "name">): ActivityItem {
  return {
    kind: "CHAT",
    description: "",
    state: "idle",
    harness: "claude",
    chat_id: overrides.item_id,
    last_messaged_at: null,
    is_critical: false,
    is_on_demand: false,
    always_on_reason: null,
    rss_kib: 0,
    processes: [],
    ...overrides,
  };
}

export function summary(
  status: MemoryStatus,
  items: readonly ActivityItem[],
  firstToCloseId: string | null,
): ActivitySummary {
  return {
    measured_at: "2026-10-01T12:00:00Z",
    memory: {
      limit_bytes: 8 * GIB,
      used_bytes: (status === "COMFORTABLE" ? 3.4 : status === "TIGHT" ? 6.4 : 7.4) * GIB,
      status,
      source: "CGROUP",
      source_detail: "cgroup",
      closes_at_used_bytes: 8 * GIB * 0.9,
      closer: "MEMORY_GUARD",
      min_available_percent: 10,
      closing_detail: "earlyoom acts once less than 10% of the memory it reads is available",
    },
    chats: items.filter((entry) => entry.kind === "CHAT" || entry.kind === "HELPER_AGENT" || entry.kind === "AGENT"),
    are_chat_names_known: true,
    apps: items.filter((entry) => entry.kind === "APP"),
    services: items.filter((entry) => entry.kind === "SERVICE" || entry.kind === "PLUMBING"),
    are_programs_known: true,
    likely_first_to_close:
      firstToCloseId === null ? null : { item_id: firstToCloseId, pid: 1, command_name: "claude" },
    notes: [],
  };
}

export const LOADING_HISTORY: HistoryChartAttrs = { state: { kind: "loading" }, range: "DAY", onRange: () => {} };

export const HOUR_END_SECONDS = Date.parse("2026-10-01T12:00:00Z") / 1000;

export function period(startEpochSeconds: number, averageKib: number, minKib: number, maxKib: number): HistoryPeriod {
  return {
    start_epoch_seconds: startEpochSeconds,
    average_kib: averageKib,
    min_kib: minKib,
    max_kib: maxKib,
    sample_count: 1,
  };
}

export function closure(at: string, what: string, freedKib: number | null): ClosedProcess {
  return {
    at,
    kind: "CHAT_AGENT",
    what,
    next_step: "Its conversation is kept; send it a message to carry on.",
    freed_kib: freedKib,
    command_name: "claude",
    pid: 4242,
    agent_name: "agent-1",
  };
}

export function historyView(overrides: Partial<HistoryView>): HistoryView {
  return {
    range: "HOUR",
    span_seconds: 3600,
    period_seconds: 60,
    end_epoch_seconds: HOUR_END_SECONDS,
    periods: [],
    limit_kib: 8 * 1024 * 1024,
    closes_at_kib: 0.9 * 8 * 1024 * 1024,
    closer: "MEMORY_GUARD",
    closures_in_range: [],
    recent_closures: [],
    is_recording: true,
    first_sample_epoch_seconds: HOUR_END_SECONDS - 7200,
    history_path: "data/.state/activity/memory-history.tsv",
    ledger_path: "data/.state/oom_priority/events/shed.jsonl",
    ...overrides,
  };
}
