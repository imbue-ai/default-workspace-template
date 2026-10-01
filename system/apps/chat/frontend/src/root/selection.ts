/**
 * The chat root's URL: the selected chat rides the ``chat`` query parameter, so the root's path
 * is ``/?chat=<id>`` (what it reports as its location) and ``/`` with nothing selected. An
 * ``intake`` parameter names a pending intake the root applies once (post-launch-paths plan
 * section 3.6): a draft for a composer, a choice of chat, or a first message that needs an
 * account; the root then reports the selection alone.
 */

// A chat id is its first agent's id (the backend's ``AGENT_ID_PATTERN`` in ``primitives.py``).
const AGENT_ID_PATTERN = /^agent-[A-Za-z0-9_-]{1,120}$/;

export const CHAT_QUERY_KEY = "chat";
export const INTAKE_QUERY_KEY = "intake";

/** The chat the URL selects, or null for none (or an id that cannot be a chat's). */
export function selectionFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get(CHAT_QUERY_KEY);
  if (value === null || !AGENT_ID_PATTERN.test(value)) return null;
  return value;
}

/** The pending intake the URL carries for the root to apply, or null for none. */
export function intakeTokenFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get(INTAKE_QUERY_KEY);
  return value === null || value === "" ? null : value;
}

/** The root's path for a selection: what it reports to the shell and writes into its own URL. */
export function rootPathFor(chatId: string | null): string {
  if (chatId === null) return "/";
  const params = new URLSearchParams();
  params.set(CHAT_QUERY_KEY, chatId);
  return `/?${params.toString()}`;
}

/** What the root does about its chat slot: keep what it shows, select a chat, or open one awaiting its first send. */
export type SlotFill = { kind: "keep" } | { kind: "select"; chatId: string } | { kind: "open_new" };

export interface SlotState {
  selectedChatId: string | null;
  /** The listed chats, most recent first. */
  chatIds: readonly string[];
  isChatListKnown: boolean;
  /** An intake is choosing the chat, or the user is picking one. */
  isChoosing: boolean;
  /** The phone layout, where the list alone is the page while nothing is selected. */
  isCompact: boolean;
  isShown: boolean;
}

/** The chat list is never left open empty: with nothing selected it shows the most recent chat, and with no chats a
 *  new one awaiting its first send. That new one is only asked for while the root is on screen, so a list open on
 *  a desktop nobody is looking at does not add one. */
export function slotFill(state: SlotState): SlotFill {
  if (state.selectedChatId !== null || !state.isChatListKnown || state.isChoosing || state.isCompact) {
    return { kind: "keep" };
  }
  const mostRecent = state.chatIds[0];
  if (mostRecent !== undefined) return { kind: "select", chatId: mostRecent };
  return state.isShown ? { kind: "open_new" } : { kind: "keep" };
}
