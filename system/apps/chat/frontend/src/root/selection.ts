/**
 * The chat root's URL: the selected chat rides the ``chat`` query parameter, so the root's path
 * is ``/?chat=<id>`` (what it reports as its location) and ``/`` with nothing selected. An
 * ``intake`` parameter names a pending intake the root applies once (post-launch-paths plan
 * section 3.6): a draft for a composer, a choice of chat, or a first message that needs an
 * account; the root then reports the selection alone. ``open=providers`` asks the root to open the
 * provider chooser once on arrival (the Imbue Studio app's sign-in button), and
 * ``open=new-chat-pointer`` to draw the coach mark at its New chat button (another app's page asked
 * the chat app for it); each too is dropped from the path the root reports.
 */

// A chat id is its first agent's id (the backend's ``AGENT_ID_PATTERN`` in ``primitives.py``).
const AGENT_ID_PATTERN = /^agent-[A-Za-z0-9_-]{1,120}$/;

export const CHAT_QUERY_KEY = "chat";
export const INTAKE_QUERY_KEY = "intake";
export const OPEN_QUERY_KEY = "open";
export const OPEN_PROVIDERS = "providers";
export const OPEN_NEW_CHAT_POINTER = "new-chat-pointer";
const VIEW_QUERY_KEY = "view";
const VIEW_FIRST_RUN = "first-run";

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

/** Whether the URL asks the root to open the provider chooser on arrival. */
export function isProviderChooserRequested(search: string): boolean {
  return new URLSearchParams(search).get(OPEN_QUERY_KEY) === OPEN_PROVIDERS;
}

/** Whether the URL asks the root to point its coach mark at the New chat button. */
export function isNewChatPointerRequested(search: string): boolean {
  return new URLSearchParams(search).get(OPEN_QUERY_KEY) === OPEN_NEW_CHAT_POINTER;
}

/** Whether the URL asks for the first run's provider page in place of the chats (the shell's cover frames it). */
export function isFirstRunProvidersViewRequested(search: string): boolean {
  return new URLSearchParams(search).get(VIEW_QUERY_KEY) === VIEW_FIRST_RUN;
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
  /** The listed chats in display order: the main chat first, then the most recent. */
  chatIds: readonly string[];
  isChatListKnown: boolean;
  /** An intake is choosing the chat, or the user is picking one. */
  isChoosing: boolean;
  /** The phone layout, where the list's drawer is open over the empty chat while nothing is selected. */
  isCompact: boolean;
  isShown: boolean;
  /** A delete is on its way, so a chat hidden from the list may still exist or come back. */
  isDeletePending: boolean;
  /** An account is signed in that a new chat could start on, and no sign-in is waiting to start a chat that has not
   *  appeared yet (``models/firstSendChooser.ts``). */
  canStartNew: boolean;
}

/** The chat list is never left open empty while it can start a chat: with nothing selected (opened bare, or the chat
 *  on screen was deleted) it shows the head of the list -- the main chat, else the most recent -- and with no chats a
 *  new one awaiting its first send. That new one is only asked for while the root is on screen, so a list open on a
 *  desktop nobody is looking at does not add one; only once an account is signed in: before that the provider
 *  chooser stands over the empty slot, and a new workspace's welcome chat lands alone rather than beside an empty
 *  one; and only once no delete is on its way: the chat app answers that ask with a chat already waiting for its
 *  first send, which could be the one being deleted. */
export function slotFill(state: SlotState): SlotFill {
  if (state.selectedChatId !== null || !state.isChatListKnown || state.isChoosing || state.isCompact) {
    return { kind: "keep" };
  }
  const head = state.chatIds[0];
  if (head !== undefined) return { kind: "select", chatId: head };
  return state.isShown && !state.isDeletePending && state.canStartNew ? { kind: "open_new" } : { kind: "keep" };
}
