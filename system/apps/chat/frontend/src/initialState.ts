/**
 * What the chat app puts in a chat's own page so its first frame needs no round trip
 * (``documents.py``'s ``inject_initial_state_script``): the socket's connect-time replay, the
 * account list and the harness catalogs, read when the page was served. Each part is the shape
 * its live source sends, and each source still runs: the socket and the fetches refresh them.
 */

export interface InitialState {
  /** The messages the socket sends on connecting, in order: provisional chats, then the chat list. */
  replay?: unknown[];
  /** What ``GET /api/accounts`` answers; absent when the account index could not be read. */
  accounts?: unknown;
  /** What ``GET /api/harnesses`` answers. */
  harnesses?: Record<string, unknown>;
}

const INITIAL_STATE_SCRIPT_ID = "chat-initial-state";

/** The page's initial state, or an empty one when it carries none (the root page, a subagent view) or it is unreadable. */
export function readInitialState(): InitialState {
  const text = document.getElementById(INITIAL_STATE_SCRIPT_ID)?.textContent ?? "";
  if (text === "") return {};
  try {
    const parsed: unknown = JSON.parse(text);
    return parsed !== null && typeof parsed === "object" ? (parsed as InitialState) : {};
  } catch (error) {
    console.warn("The page's initial state could not be read; waiting on the chat app instead", error);
    return {};
  }
}
