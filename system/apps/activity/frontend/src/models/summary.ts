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

// A read that hangs (a stuck worker, a proxy holding the connection) must fail, or the poller, which waits for each
// read before scheduling the next, would freeze on old figures with nothing saying so. The backend's own slowest
// dependency answers within 5 s.
export const SUMMARY_TIMEOUT_MS = 15_000;
// A stop or start runs mngr in the chat app, which the backend allows 60 s.
export const CHAT_ACTION_TIMEOUT_MS = 70_000;

// The workspace's clock minus this browser's, from the latest summary: idle times are the workspace's own
// timestamps, so they are measured against its clock, not a laptop's that may be minutes off.
let serverClockOffsetMs = 0;

export function serverNowMs(): number {
  return Date.now() + serverClockOffsetMs;
}

function failureMessage(error: unknown): string {
  return error instanceof DOMException && error.name === "TimeoutError"
    ? "It took too long to answer."
    : String(error);
}

/** Keep the figures shown, but remember since when they could not be refreshed, and the latest reason why. */
function noteFailure(message: string): void {
  if (state.kind === "loaded") refreshFailure = { since: refreshFailure?.since ?? Date.now(), message };
  else state = { kind: "failed", message };
}

/** Read the summary once; false once the page is forbidden, since reading again cannot change that. */
async function readSummary(): Promise<boolean> {
  latestReadNumber += 1;
  const readNumber = latestReadNumber;
  try {
    const response = await fetch(SUMMARY_PATH, { cache: "no-store", signal: AbortSignal.timeout(SUMMARY_TIMEOUT_MS) });
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
    serverClockOffsetMs = Date.parse(summary.measured_at) - Date.now();
  } catch (error) {
    if (readNumber === latestReadNumber) noteFailure(failureMessage(error));
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
      signal: AbortSignal.timeout(CHAT_ACTION_TIMEOUT_MS),
    });
    if (response.ok) return { kind: "done" };
    const body = (await response.json().catch(() => ({}))) as { detail?: string; chat_status?: string };
    if (response.status === 409 && body.chat_status === "working") return { kind: "started_working" };
    return { kind: "failed", message: body.detail ?? `The chat app answered ${response.status}.` };
  } catch (error) {
    const isTimeout = error instanceof DOMException && error.name === "TimeoutError";
    return {
      kind: "failed",
      message: isTimeout ? "It took too long to answer; it may still be under way." : String(error),
    };
  }
}
