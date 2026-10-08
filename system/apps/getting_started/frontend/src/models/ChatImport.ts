/**
 * The "Bring in your chats" card's state, as the app serves it at ``GET /api/chat-import``: what
 * the import-chats skill has imported from each source, and whether the user put the card away
 * (``src/getting_started/chat_import.py``).
 *
 * Fetched once per page load and again when the window regains focus; while a source is still
 * importing it is polled, so the card counts up as the import runs. A failed fetch leaves the card
 * hidden: it is an offer, and the page reads fine without it.
 *
 * ``cardPhase`` (which face the card shows) and ``importedSummary`` are pure and tested.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export type ChatImportSourceState = "importing" | "imported" | "needs_sign_in" | "failed";

export interface ChatImportSource {
  state: ChatImportSourceState;
  conversations: number;
  updated_at: string;
  detail: string;
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

// pure helpers
export function cardPhase(chatImport: ChatImport | null): ChatImportCardPhase {
  if (chatImport === null || chatImport.is_dismissed) return "hidden";
  const states = Object.values(chatImport.sources).map((source) => source.state);
  if (states.length === 0) return "offer";
  if (states.includes("importing")) return "importing";
  if (states.some((state) => state === "needs_sign_in" || state === "failed")) return "attention";
  return "imported";
}

/** "312 from Claude and 40 from ChatGPT": what each recorded source holds, in the card's order. */
export function importedSummary(sources: Record<string, ChatImportSource>): string {
  const parts = SOURCE_LABELS.flatMap(([key, label]) => {
    const source = sources[key];
    return source === undefined ? [] : [`${source.conversations.toLocaleString()} from ${label}`];
  });
  if (parts.length <= 1) return parts.join("");
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

// the fetch, the poll, and the dismissal
let state: ChatImport | null = null;
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

async function fetchChatImport(init?: RequestInit, path: string = CHAT_IMPORT_PATH): Promise<ChatImport | null> {
  try {
    const response = await fetch(apiUrl(path), init);
    if (!response.ok) {
      console.warn(`[getting-started] could not load the chat import state: HTTP ${response.status}`);
      return null;
    }
    return (await response.json()) as ChatImport;
  } catch (e) {
    console.warn("[getting-started] could not load the chat import state", e);
    return null;
  }
}

/** Fetch the card's state; keeps polling for as long as a source is importing. */
export async function refreshChatImport(): Promise<void> {
  const next = await fetchChatImport();
  if (next !== null) state = next;
  m.redraw();
  schedulePoll();
}

/** Put the card away for good; it hides at once, whatever the server answers. */
export async function dismissChatImport(): Promise<void> {
  if (state !== null) state = { ...state, is_dismissed: true };
  m.redraw();
  const next = await fetchChatImport({ method: "POST" }, CHAT_IMPORT_DISMISS_PATH);
  if (next !== null) state = next;
  schedulePoll();
}
