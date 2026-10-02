/** Summary records the tests build pages and questions from. */

import type { ActivityItem, ActivitySummary, MemoryStatus } from "../models/summary";

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
    is_preview: false,
  };
}
