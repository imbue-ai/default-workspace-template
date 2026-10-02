/**
 * The memory tab's data: the backend's ``GET /api/summary`` document (``activity.summary.ActivitySummary``), read
 * while the window is shown, and the stop and start a chat can be asked for through the chat app.
 */

import m from "mithril";
import { createPoller } from "./poller";

export type MemoryStatus = "COMFORTABLE" | "TIGHT" | "CRITICAL";
export type MemorySource = "HOST_MEMINFO" | "CGROUP" | "PROC_MEMINFO";
export type MemoryCloser = "MEMORY_GUARD" | "SYSTEM_LIMIT";
export type ItemKind = "CHAT" | "HELPER_AGENT" | "AGENT" | "APP" | "SERVICE" | "PLUMBING";

export interface ProcessView {
  readonly pid: number;
  readonly command_name: string;
  readonly command_line: string;
  readonly rss_kib: number;
  readonly oom_score_adj: number;
}

export interface ActivityItem {
  readonly item_id: string;
  readonly kind: ItemKind;
  readonly name: string;
  readonly description: string;
  readonly state: string;
  readonly harness: string | null;
  readonly chat_id: string | null;
  readonly last_messaged_at: number | null;
  readonly is_critical: boolean;
  readonly is_on_demand: boolean;
  readonly always_on_reason: string | null;
  readonly app_name: string | null;
  readonly is_stoppable: boolean;
  readonly is_restarted_on_open: boolean;
  readonly rss_kib: number;
  readonly processes: readonly ProcessView[];
}

export interface MemorySummary {
  readonly limit_bytes: number;
  readonly used_bytes: number;
  readonly status: MemoryStatus;
  readonly source: MemorySource;
  readonly source_detail: string;
  readonly closes_at_used_bytes: number;
  readonly closer: MemoryCloser;
  readonly min_available_percent: number | null;
  readonly closing_detail: string;
}

export interface LikelyFirstToClose {
  readonly item_id: string;
  readonly pid: number;
  readonly command_name: string;
}

export interface ActivitySummary {
  readonly measured_at: string;
  readonly memory: MemorySummary | null;
  readonly chats: readonly ActivityItem[];
  readonly are_chat_names_known: boolean;
  readonly apps: readonly ActivityItem[];
  readonly services: readonly ActivityItem[];
  readonly are_programs_known: boolean;
  readonly likely_first_to_close: LikelyFirstToClose | null;
  readonly notes: readonly string[];
  readonly is_preview: boolean;
}

export type SummaryState =
  | { readonly kind: "loading" }
  | { readonly kind: "loaded"; readonly summary: ActivitySummary }
  | { readonly kind: "failed"; readonly message: string }
  | { readonly kind: "forbidden" };

/** A refresh that failed after the page had loaded: the figures on screen are from before ``since``. */
export interface RefreshFailure {
  readonly since: number;
  readonly message: string;
}

export const SUMMARY_PATH = "/api/summary";
// How often the page re-reads while its window is shown; it reads nothing while hidden.
export const REFRESH_INTERVAL_MS = 5000;

let state: SummaryState = { kind: "loading" };
let refreshFailure: RefreshFailure | null = null;
// Each read's number, so a slower earlier read never overwrites a later one.
let latestReadNumber = 0;

export function getSummaryState(): SummaryState {
  return state;
}

export function getRefreshFailure(): RefreshFailure | null {
  return refreshFailure;
}

/** Keep the figures shown, but remember since when they could not be refreshed. */
function noteFailure(message: string): void {
  if (state.kind === "loaded") refreshFailure = refreshFailure ?? { since: Date.now(), message };
  else state = { kind: "failed", message };
}

/** Read the summary once; false once the page is forbidden, since reading again cannot change that. */
async function readSummary(): Promise<boolean> {
  latestReadNumber += 1;
  const readNumber = latestReadNumber;
  try {
    const response = await fetch(SUMMARY_PATH, { cache: "no-store" });
    if (readNumber !== latestReadNumber) return true;
    if (response.status === 403) {
      state = { kind: "forbidden" };
      return false;
    }
    if (!response.ok) {
      // A 503 is the shell's "starting" page while the app wakes up; the next read will likely succeed.
      noteFailure(`The page answered ${response.status}.`);
      return true;
    }
    const summary = (await response.json()) as ActivitySummary;
    if (readNumber !== latestReadNumber) return true;
    state = { kind: "loaded", summary };
    refreshFailure = null;
  } catch (error) {
    if (readNumber === latestReadNumber) noteFailure(String(error));
  } finally {
    m.redraw();
  }
  return true;
}

const poller = createPoller(readSummary, REFRESH_INTERVAL_MS);

/** Read now and every few seconds after, until ``stopRefreshing``. */
export function startRefreshing(): void {
  poller.start();
}

export function stopRefreshing(): void {
  poller.stop();
}

export async function refreshNow(): Promise<void> {
  await readSummary();
}

export type ChatActionResult =
  | { readonly kind: "done" }
  | { readonly kind: "started_working" }
  | { readonly kind: "failed"; readonly message: string };

/**
 * Ask the chat app, through this app, to stop or start a chat. A stop that would interrupt a turn the user did not
 * confirm comes back ``started_working``, so the page can ask again with the chat's new state.
 */
export async function requestChatAction(
  chatId: string,
  action: "stop" | "start",
  isInterruptConfirmed: boolean,
): Promise<ChatActionResult> {
  try {
    const response = await fetch(`/api/chats/${encodeURIComponent(chatId)}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_interrupt_confirmed: isInterruptConfirmed }),
    });
    if (response.ok) return { kind: "done" };
    const body = (await response.json().catch(() => ({}))) as { detail?: string; chat_status?: string };
    if (response.status === 409 && body.chat_status === "working") return { kind: "started_working" };
    return { kind: "failed", message: body.detail ?? `The chat app answered ${response.status}.` };
  } catch (error) {
    return { kind: "failed", message: String(error) };
  }
}

export type AppStopResult = { readonly kind: "done" } | { readonly kind: "failed"; readonly message: string };

/** Ask the desktop, through this app, to quit an app: its windows close, and it starts again when next opened. */
export async function requestAppStop(appName: string): Promise<AppStopResult> {
  try {
    const response = await fetch(`/api/apps/${encodeURIComponent(appName)}/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (response.ok) return { kind: "done" };
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    return { kind: "failed", message: body.detail ?? `The page answered ${response.status}.` };
  } catch (error) {
    return { kind: "failed", message: String(error) };
  }
}
