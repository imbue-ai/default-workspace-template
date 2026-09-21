/**
 * The one text match of the workspace's typeaheads: the desktop's launcher (its launch paths and
 * windows), the Getting Started page's search (its intents and templates), and the chat's send
 * picker (its chats) all narrow a list the same way.
 */

/**
 * Whether every whitespace-separated token of ``query`` appears somewhere in ``fields``, case-
 * insensitively -- so "open term" finds "Open new terminal" and word order does not matter. Plain
 * substrings, no fuzz: at the page's size a near miss confuses more than the extra typing costs.
 */
export function matchesQuery(query: string, ...fields: readonly string[]): boolean {
  const haystack = fields.join(" ").toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter((token) => token !== "")
    .every((token) => haystack.includes(token));
}
