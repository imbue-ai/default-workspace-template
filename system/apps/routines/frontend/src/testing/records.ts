/** The API's records as the view tests build them: one of each, patched per test. */

import type {
  RoutineDefinition,
  RoutineDetail,
  RoutineListing,
  RoutineRow,
  RunDetail,
  RunRecord,
} from "../models/api";
import type { ScheduleForm } from "../models/schedule";

export function rowOf(patch: Partial<RoutineRow> = {}): RoutineRow {
  return {
    routine_id: "news-sync",
    title: "News sync",
    what: "Pulls the headlines.",
    health: "WORKING",
    health_text: "Working",
    health_detail: "",
    is_running: false,
    is_enabled: true,
    kind: "COMMAND",
    app: null,
    schedule_text: "Every hour",
    next_due_at: null,
    next_run_text: "today 8:00 am",
    last_run: null,
    last_run_text: "Worked 10 minutes ago, automatic",
    error: null,
    skill: null,
    command: ["python3", "sync.py"],
    chat_id: null,
    ...patch,
  };
}

export function listingOf(rows: RoutineRow[], appNames: Record<string, string> = {}): RoutineListing {
  return {
    routines: rows,
    summary: {
      total: rows.length,
      needs_attention: rows.filter((row) => row.health === "FAILING").length,
      running: rows.filter((row) => row.is_running).length,
      next_run_text: "today 8:00 am",
    },
    app_names: appNames,
    generated_at: "2026-10-20T15:00:00+00:00",
  };
}

export function definitionOf(patch: Partial<RoutineDefinition> = {}): RoutineDefinition {
  return {
    title: "News sync",
    what: "Pulls the headlines.",
    every: "1h",
    at: null,
    days: [],
    day_of_month: null,
    once: null,
    command: ["python3", "sync.py"],
    skill: null,
    enabled: true,
    timeout: "1h",
    retry_after: "2m",
    notify_after_failures: 3,
    app: null,
    ...patch,
  };
}

export function runOf(patch: Partial<RunRecord> = {}): RunRecord {
  return {
    run_id: "run-1",
    routine_id: "news-sync",
    trigger: "SCHEDULED",
    kind: "COMMAND",
    outcome: "SUCCEEDED",
    started_at: "2026-10-20T14:50:00+00:00",
    finished_at: "2026-10-20T14:50:12+00:00",
    duration_seconds: 12,
    summary: "3 headlines pulled",
    chat_id: null,
    warnings: [],
    is_blocked: false,
    exit_code: 0,
    when_text: "10 minutes ago",
    duration_text: "12s",
    outcome_text: "Worked",
    ...patch,
  };
}

export function detailOf(patch: Partial<RoutineDetail> = {}): RoutineDetail {
  return {
    row: rowOf(),
    definition: definitionOf(),
    definition_path: "system/routines/news-sync.toml",
    runs: [],
    ...patch,
  };
}

export function runDetailOf(patch: Partial<RunDetail> = {}): RunDetail {
  return { run: runOf(), log: "pulling...\ndone\n", is_log_truncated: false, links: [], ...patch };
}

/** The schedule editor's form as the tests start it: daily at 7:30 on Mondays. */
export function scheduleFormOf(patch: Partial<ScheduleForm> = {}): ScheduleForm {
  return { mode: "every", count: "1", unit: "d", at: "07:30", days: ["mon"], dayOfMonth: "", once: "", ...patch };
}
