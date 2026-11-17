/**
 * The System Monitor's API (system/apps/system_monitor/src/system_monitor/runner.py), whose shapes are
 * ``data_types.py``'s: the reading every view but the disk polls, the longer graph spans, the disk view's
 * figures, the walk of the disk, and the Stop buttons' request.
 */

export interface CpuFigures {
  usage: number;
  cores: number;
  processes: number;
  sampled_at: number | null;
}

export interface MemoryFigures {
  total: number;
  available: number;
  used: number;
}

export interface DiskFigures {
  total: number;
  used: number;
  free: number;
}

export type StopKind = "chat" | "checkers" | "app";

export interface StopTarget {
  kind: StopKind;
  id: string;
}

export interface UsageItem {
  name: string;
  bytes: number;
  cpu?: number | null;
  stop?: StopTarget | null;
}

export interface UsageRow {
  id: string;
  label: string;
  hint: string;
  bytes: number;
  cpu?: number | null;
  items: UsageItem[];
  limit?: number | null;
}

export interface StopRecord {
  at: string;
  what: string;
  command: string | null;
}

export interface StretchCause {
  name: string;
  share: number;
}

export interface EventRecord {
  at: string;
  kind: "stop" | "busy";
  what: string;
  duration_seconds?: number | null;
  top?: StretchCause[];
}

export interface HistorySample {
  t: number;
  cpu: number | null;
  memory: number | null;
}

export interface StatsResponse {
  cpu: CpuFigures;
  memory: MemoryFigures;
  disk: DiskFigures;
  usage: UsageRow[];
  usage_mode: "exact" | "scaled";
  stops: StopRecord[];
  events: EventRecord[];
  history: HistorySample[];
}

export type Span = "10m" | "1h" | "24h";
export type LongSpan = Exclude<Span, "10m">;

export interface DiskItem {
  name: string;
  hint?: string;
  bytes: number;
}

export interface DiskRow {
  id: string;
  label: string;
  hint: string;
  bytes: number;
  items: DiskItem[];
}

export interface DiskScan {
  state: "idle" | "scanning" | "done" | "failed";
  scanned_at: string | null;
  rows: DiskRow[];
  missing: string[];
}

export type BackupState = "not_configured" | "running" | "up_to_date" | "stale" | "failing" | "out_of_storage";

export interface BackupSummary {
  state: BackupState;
  last_success_at: string | null;
  last_failure_at: string | null;
  interval_seconds: number;
  failures_last_day: number;
  readme_path: string;
}

export interface DiskResponse {
  disk: DiskFigures;
  backups: BackupSummary;
  scan: DiskScan;
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(String(response.status));
  return (await response.json()) as T;
}

/** The latest reading; throws when the app cannot be reached or refuses. */
export function fetchStats(): Promise<StatsResponse> {
  return getJson<StatsResponse>("/api/stats");
}

/** The disk, the backups, and the walk's state; throws when the app cannot be reached or refuses. */
export function fetchDisk(): Promise<DiskResponse> {
  return getJson<DiskResponse>("/api/disk");
}

/** A longer span of both graphs; none on a refusal. */
export async function fetchHistory(span: LongSpan): Promise<HistorySample[]> {
  const response = await fetch(`/api/history?span=${span}`, { cache: "no-store" });
  if (!response.ok) return [];
  return ((await response.json()) as { samples: HistorySample[] }).samples;
}

/** Ask for a walk of the disk (the app starts one unless one is running). */
export async function startDiskScan(): Promise<void> {
  await fetch("/api/disk/scan", { method: "POST" });
}

/** Stop what a Stop button names: "" once it is stopped, else what went wrong, in words to show. */
export async function stopTarget(target: StopTarget, verb: string): Promise<string> {
  try {
    const response = await fetch("/api/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: target.kind, id: target.id }),
    });
    if (response.ok) return "";
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    return body.detail || `Could not ${verb.toLowerCase()} it (${response.status})`;
  } catch {
    return `Could not ${verb.toLowerCase()} it: the workspace did not answer`;
  }
}
