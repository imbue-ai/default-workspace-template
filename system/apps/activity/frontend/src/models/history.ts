/**
 * The memory-over-time chart's data: the backend's ``GET /api/history`` document (``activity.history_view``), read
 * when the memory tab is shown and once a minute while it stays shown, since readings are a minute apart.
 */

import m from "mithril";
import { createPoller } from "./poller";

export type HistoryRange = "HOUR" | "DAY" | "WEEK";
export type ClosedKind = "CHAT_AGENT" | "HELPER_AGENT" | "BROWSER_TAB" | "SERVICE" | "PROGRAM";

export interface HistoryPeriod {
  readonly start_epoch_seconds: number;
  readonly average_kib: number;
  readonly min_kib: number;
  readonly max_kib: number;
  readonly sample_count: number;
}

export interface ClosedProcess {
  readonly at: string;
  readonly kind: ClosedKind;
  readonly what: string;
  readonly next_step: string;
  readonly freed_kib: number | null;
  readonly command_name: string;
  readonly pid: number;
  readonly agent_name: string | null;
}

export interface HistoryView {
  readonly range: HistoryRange;
  readonly span_seconds: number;
  readonly period_seconds: number;
  readonly end_epoch_seconds: number;
  readonly periods: readonly HistoryPeriod[];
  readonly limit_kib: number | null;
  readonly closes_at_kib: number | null;
  readonly closer: "MEMORY_GUARD" | "SYSTEM_LIMIT" | null;
  readonly closures_in_range: readonly ClosedProcess[];
  readonly recent_closures: readonly ClosedProcess[];
  readonly is_recording: boolean;
  readonly first_sample_epoch_seconds: number | null;
  readonly history_path: string;
  readonly ledger_path: string;
}

export type HistoryState =
  | { readonly kind: "loading" }
  | { readonly kind: "loaded"; readonly view: HistoryView }
  | { readonly kind: "failed"; readonly message: string };

export const HISTORY_PATH = "/api/history";
export const HISTORY_REFRESH_MS = 60_000;

let state: HistoryState = { kind: "loading" };
let selectedRange: HistoryRange = "DAY";

export function getHistoryState(): HistoryState {
  return state;
}

export function getSelectedRange(): HistoryRange {
  return selectedRange;
}

async function readHistory(): Promise<boolean> {
  const range = selectedRange;
  try {
    const response = await fetch(`${HISTORY_PATH}?range=${range.toLowerCase()}`, { cache: "no-store" });
    // A reply for a range the user has since left is dropped, so a slow one never replaces the chart they picked.
    if (range !== selectedRange) return true;
    if (!response.ok) {
      if (state.kind !== "loaded") state = { kind: "failed", message: `The page answered ${response.status}.` };
      return true;
    }
    const view = (await response.json()) as HistoryView;
    if (range === selectedRange) state = { kind: "loaded", view };
  } catch (error) {
    if (range === selectedRange && state.kind !== "loaded") state = { kind: "failed", message: String(error) };
  } finally {
    m.redraw();
  }
  return true;
}

const poller = createPoller(readHistory, HISTORY_REFRESH_MS);

export function startHistoryRefreshing(): void {
  poller.start();
}

export function stopHistoryRefreshing(): void {
  poller.stop();
}

export function selectRange(range: HistoryRange): void {
  if (range === selectedRange) return;
  selectedRange = range;
  void readHistory();
}
