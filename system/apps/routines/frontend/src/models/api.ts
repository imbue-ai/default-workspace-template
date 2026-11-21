/**
 * The Routines app's API (system/apps/routines/src/routines_app/server.py), whose shapes are the routines
 * library's own models: the listing, a routine with its runs, a run with its log, and the mutations.
 */

import { HttpError, errorDetailFromResponse, requestJson } from "@imbue/workspace-ui/src/models/http";

export type Health = "BROKEN" | "OFF" | "DONE" | "NEEDS_USER" | "FAILING" | "OVERDUE" | "NOT_RUN_YET" | "WORKING";
export type Trigger = "SCHEDULED" | "MANUAL";
export type Kind = "COMMAND" | "SKILL";
export type Outcome = "SUCCEEDED" | "FAILED" | "TIMED_OUT" | "INTERRUPTED";
export type RunWarning = "RESTARTED_PREVIOUS_RUN";

export interface RunRecord {
  run_id: string;
  routine_id: string;
  trigger: Trigger;
  kind: Kind;
  outcome: Outcome | null;
  started_at: string;
  finished_at: string | null;
  duration_seconds: number | null;
  summary: string;
  chat_id: string | null;
  warnings: RunWarning[];
  is_blocked: boolean;
  exit_code: number | null;
  when_text: string;
  duration_text: string;
  outcome_text: string;
}

export interface RoutineRow {
  routine_id: string;
  title: string;
  what: string;
  health: Health;
  health_text: string;
  health_detail: string;
  is_running: boolean;
  is_enabled: boolean;
  kind: Kind | null;
  app: string | null;
  schedule_text: string;
  next_due_at: string | null;
  next_run_text: string;
  last_run: RunRecord | null;
  last_run_text: string;
  error: string | null;
  skill: string | null;
  command: string[];
  chat_id: string | null;
  /** The app whose directory holds the definition file, by name; null for a routine the workspace owns. */
  owning_app: string | null;
  /** Whether the schedule and the on/off switch are all that can be changed here: the rest is the app's. */
  is_schedule_only: boolean;
  /** Whether the routine runs on a schedule the user gave it in place of its app's. */
  has_schedule_override: boolean;
  /** The app's own schedule in words while the user's is in its place; null otherwise. */
  app_schedule_text: string | null;
}

/** The app a routine is shown under: the one that owns its definition file, else the one its definition
 * names for grouping. */
export function appOf(row: RoutineRow): string | null {
  return row.owning_app ?? row.app;
}

export interface ListingSummary {
  total: number;
  needs_attention: number;
  running: number;
  next_run_text: string;
}

export interface RoutineListing {
  routines: RoutineRow[];
  summary: ListingSummary;
  /** The display name of each registered app, by name, for the group headings. */
  app_names: Record<string, string>;
  generated_at: string;
}

export interface RoutineDefinition {
  title: string;
  what: string;
  every: string | null;
  at: string | null;
  days: string[];
  day_of_month: number | null;
  once: string | null;
  command: string[] | null;
  skill: string | null;
  enabled: boolean;
  timeout: string;
  retry_after: string;
  notify_after_failures: number;
  app: string | null;
}

export interface RoutineDetail {
  row: RoutineRow;
  definition: RoutineDefinition | null;
  definition_path: string;
  runs: RunRecord[];
}

export interface ResultLink {
  label: string;
  url: string;
}

export interface RunDetail {
  run: RunRecord;
  log: string;
  is_log_truncated: boolean;
  links: ResultLink[];
}

/** The schedule fields a routine is rescheduled with, as the API takes them. */
export interface ScheduleChange {
  every: string | null;
  at: string | null;
  days: string[];
  day_of_month: number | null;
  once: string | null;
}

export interface RunStartReceipt {
  routine_id: string;
  run_id: string;
}

export interface FixChatResponse {
  chat_id: string;
}

const encode = encodeURIComponent;

export function fetchListing(): Promise<RoutineListing> {
  return requestJson("GET", "/api/routines");
}

export function fetchDetail(routineId: string): Promise<RoutineDetail> {
  return requestJson("GET", `/api/routines/${encode(routineId)}`);
}

export function fetchRun(routineId: string, runId: string): Promise<RunDetail> {
  return requestJson("GET", `/api/routines/${encode(routineId)}/runs/${encode(runId)}`);
}

export function runNow(routineId: string): Promise<RunStartReceipt> {
  return requestJson("POST", `/api/routines/${encode(routineId)}/run`, {});
}

export function setEnabled(routineId: string, isEnabled: boolean): Promise<RoutineRow> {
  return requestJson("PUT", `/api/routines/${encode(routineId)}/enabled`, { is_enabled: isEnabled });
}

export function setSchedule(routineId: string, change: ScheduleChange): Promise<RoutineRow> {
  return requestJson("PUT", `/api/routines/${encode(routineId)}/schedule`, change);
}

/** Go back to the schedule the routine's app gives it, dropping the one the user gave it. */
export function clearScheduleOverride(routineId: string): Promise<RoutineRow> {
  return requestJson("DELETE", `/api/routines/${encode(routineId)}/schedule`);
}

export function openFixChat(routineId: string): Promise<FixChatResponse> {
  return requestJson("POST", `/api/routines/${encode(routineId)}/fix`, {});
}

export function removeRoutine(routineId: string): Promise<void> {
  return requestJson("DELETE", `/api/routines/${encode(routineId)}`);
}

/** Where a run's whole log is served as text. */
export function logUrlOf(routineId: string, runId: string): string {
  return `/api/routines/${encode(routineId)}/runs/${encode(runId)}/log`;
}

/** A run's whole log, as the text the routine printed. */
export async function fetchLog(routineId: string, runId: string): Promise<string> {
  const response = await fetch(logUrlOf(routineId, runId), { cache: "no-store" });
  if (!response.ok) throw new HttpError(response.status, await errorDetailFromResponse(response));
  return response.text();
}
