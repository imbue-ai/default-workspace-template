/**
 * The chat root's URL: the selected chat rides the ``chat`` query parameter, so the root's path
 * is ``/?chat=<id>`` (what it reports as its location) and ``/`` with nothing selected.
 * ``/new`` is the launch path: the root with a chat just created (plan section 9.1).
 */

// A chat id is its first agent's id (the backend's ``AGENT_ID_PATTERN`` in ``primitives.py``).
const AGENT_ID_PATTERN = /^agent-[A-Za-z0-9_-]{1,120}$/;

export const CHAT_QUERY_KEY = "chat";
export const NEW_CHAT_PATHNAME = "/new";
// The launch path's params (system/apps/chat/app.toml).
export const ACCOUNT_ID_PARAM = "account_id";
export const MESSAGE_PARAM = "message";

/** The chat the URL selects, or null for none (or an id that cannot be a chat's). */
export function selectionFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get(CHAT_QUERY_KEY);
  if (value === null || !AGENT_ID_PATTERN.test(value)) return null;
  return value;
}

/** The root's path for a selection: what it reports to the shell and writes into its own URL. */
export function rootPathFor(chatId: string | null): string {
  if (chatId === null) return "/";
  const params = new URLSearchParams();
  params.set(CHAT_QUERY_KEY, chatId);
  return `/?${params.toString()}`;
}

/** Whether ``pathname`` (under ``basePath``) is the ``new`` launch path. */
export function isNewChatPath(pathname: string, basePath: string): boolean {
  return pathname === `${basePath}${NEW_CHAT_PATHNAME}` || pathname === `${basePath}${NEW_CHAT_PATHNAME}/`;
}

export interface NewChatParams {
  accountId: string;
  message: string;
}

/** The launch path's params: the account to start on ("" for the default) and the first message ("" for none). */
export function newChatParamsFromSearch(search: string): NewChatParams {
  const params = new URLSearchParams(search);
  return { accountId: params.get(ACCOUNT_ID_PARAM) ?? "", message: params.get(MESSAGE_PARAM) ?? "" };
}
