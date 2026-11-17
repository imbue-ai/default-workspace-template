/** The API's records as the tests build them: one of each, patched per test. */

import type { BackupSummary, DiskResponse, DiskScan, StatsResponse, UsageRow } from "../models/api";

export const GB = 2 ** 30;
export const MB = 2 ** 20;

export function usageOf(): UsageRow[] {
  return [
    {
      id: "chats",
      label: "Chats",
      hint: "",
      bytes: 2 * GB,
      cpu: 0.05,
      items: [
        { name: "Inbox", bytes: 1.5 * GB, cpu: 0.01, stop: { kind: "chat", id: "chat-1" } },
        { name: "Report", bytes: 0.5 * GB, cpu: 0.04, stop: { kind: "chat", id: "chat-2" } },
      ],
    },
    { id: "checkers", label: "Code checkers", hint: "", bytes: 0, cpu: null, items: [] },
    {
      id: "workspace",
      label: "Workspace",
      hint: "",
      bytes: GB,
      cpu: 0.2,
      items: [{ name: "Chat app", bytes: GB, cpu: 0.2, stop: null }],
    },
    { id: "apps", label: "Your apps", hint: "", bytes: 0, cpu: null, items: [] },
    { id: "scratch", label: "Temporary files", hint: "", bytes: 200 * MB, cpu: null, items: [], limit: GB },
  ];
}

export function statsOf(patch: Partial<StatsResponse> = {}): StatsResponse {
  return {
    cpu: { usage: 0.37, cores: 4, processes: 150, sampled_at: Date.now() / 1000 - 3 },
    memory: { total: 8 * GB, available: 4 * GB, used: 4 * GB },
    disk: { total: 64 * GB, used: 16 * GB, free: 48 * GB },
    usage: usageOf(),
    usage_mode: "exact",
    stops: [],
    events: [],
    history: [
      { t: Date.now() - 10_000, cpu: 0.3, memory: 0.5 },
      { t: Date.now() - 5_000, cpu: 0.4, memory: 0.5 },
    ],
    ...patch,
  };
}

export function backupsOf(patch: Partial<BackupSummary> = {}): BackupSummary {
  return {
    state: "up_to_date",
    last_success_at: new Date(Date.now() - 30 * 60_000).toISOString(),
    last_failure_at: null,
    interval_seconds: 6 * 3600,
    failures_last_day: 0,
    readme_path: "/home/user/workspace/system/services/host_backup/README.md",
    ...patch,
  };
}

export function scanOf(patch: Partial<DiskScan> = {}): DiskScan {
  return { state: "idle", scanned_at: null, rows: [], missing: [], ...patch };
}

export function diskOf(patch: Partial<DiskResponse> = {}): DiskResponse {
  return {
    disk: { total: 64 * GB, used: 16 * GB, free: 48 * GB },
    backups: backupsOf(),
    scan: scanOf(),
    ...patch,
  };
}
