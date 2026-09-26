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
