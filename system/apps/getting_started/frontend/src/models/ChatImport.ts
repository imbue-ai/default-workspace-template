/**
 * The "Bring in your chats" card's state, as the app serves it at ``GET /api/chat-import``: what
 * the import-chats skill has imported from each source, and whether the user put the card away
 * (``src/getting_started/chat_import.py``).
 *
 * Fetched once per page load and again when the window regains focus; while a source is still
 * importing it is polled, so the card counts up as the import runs. A fetch that fails keeps what
 * was last loaded, so a failed first fetch leaves the card hidden: it is an offer, and the page
 * reads fine without it.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export type ChatImportSourceState = "importing" | "imported" | "needs_sign_in" | "failed";

export interface ChatImportSource {
  state: ChatImportSourceState;
  conversations: number;
  updated_at: string;
  detail: string;
  /** While importing: conversations fetched of ``to_fetch``. Null when the source reports no total. */
  fetched?: number | null;
  to_fetch?: number | null;
}

export interface ChatImport {
  is_dismissed: boolean;
  sources: Record<string, ChatImportSource>;
}

/** The face the card shows: the offer, the import running, the result, a source that needs the
 *  user, or nothing. */
export type ChatImportCardPhase = "offer" | "importing" | "imported" | "attention" | "hidden";

export const CHAT_IMPORT_PATH = "/api/chat-import";
export const CHAT_IMPORT_DISMISS_PATH = "/api/chat-import/dismiss";
export const POLL_INTERVAL_MS = 4000;

/** The names the sources go by on the card, in the order it lists them. */
export const SOURCE_LABELS: ReadonlyArray<readonly [string, string]> = [
  ["claude", "Claude"],
  ["chatgpt", "ChatGPT"],
];

export function cardPhase(chatImport: ChatImport | null): ChatImportCardPhase {
  if (chatImport === null || chatImport.is_dismissed) return "hidden";
  const states = Object.values(chatImport.sources).map((source) => source.state);
  if (states.length === 0) return "offer";
  if (states.includes("importing")) return "importing";
  if (states.some((state) => state === "needs_sign_in" || state === "failed")) return "attention";
  return "imported";
}

/** "a, b and c". */
function joinedList(parts: ReadonlyArray<string>): string {
  if (parts.length <= 1) return parts.join("");
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

/** "312 from Claude and 40 from ChatGPT": what each recorded source holds, in the card's order. */
export function importedSummary(sources: Record<string, ChatImportSource>): string {
  return joinedList(
    SOURCE_LABELS.flatMap(([key, label]) => {
      const source = sources[key];
      return source === undefined ? [] : [`${source.conversations.toLocaleString()} from ${label}`];
    }),
  );
}

/** "Claude and ChatGPT": the names of the sources ``keys`` holds, in the card's order; every source's
 *  when it holds none the card knows. */
export function sourceNames(keys: ReadonlyArray<string>): string {
  const known = SOURCE_LABELS.filter(([key]) => keys.includes(key));
  return joinedList((known.length > 0 ? known : SOURCE_LABELS).map(([, label]) => label));
}

let state: ChatImport | null = null;
let isDismissedHere = false;
let pollTimer: ReturnType<typeof setTimeout> | null = null;

export function getChatImport(): ChatImport | null {
  return state;
}

function schedulePoll(): void {
  if (pollTimer !== null) clearTimeout(pollTimer);
  pollTimer = null;
  if (cardPhase(state) !== "importing") return;
  pollTimer = setTimeout(() => void refreshChatImport(), POLL_INTERVAL_MS);
}

/** Take a server answer as the state. A dismissal made here is never undone by an answer to a load
 *  the server read before it. */
function adopt(next: ChatImport): void {
  state = isDismissedHere ? { ...next, is_dismissed: true } : next;
}

/** ``action`` says what the request was for in the warning a failure logs ("could not <action>"). */
async function fetchChatImport(path: string, action: string, init?: RequestInit): Promise<ChatImport | null> {
  try {
    const response = await fetch(apiUrl(path), init);
    if (!response.ok) {
      console.warn(`[getting-started] could not ${action}: HTTP ${response.status}`);
      return null;
    }
    return (await response.json()) as ChatImport;
  } catch (e) {
    console.warn(`[getting-started] could not ${action}`, e);
    return null;
  }
}

/** Fetch the card's state; keeps polling for as long as a source is importing. */
export async function refreshChatImport(): Promise<void> {
  const next = await fetchChatImport(CHAT_IMPORT_PATH, "load the chat import state");
  if (next !== null) adopt(next);
  m.redraw();
  schedulePoll();
}

/** Put the card away for good; it hides at once, whatever the server answers. */
export async function dismissChatImport(): Promise<void> {
  isDismissedHere = true;
  if (state !== null) adopt(state);
  m.redraw();
  const next = await fetchChatImport(CHAT_IMPORT_DISMISS_PATH, "put the chat import card away", { method: "POST" });
  if (next !== null) adopt(next);
  schedulePoll();
}
