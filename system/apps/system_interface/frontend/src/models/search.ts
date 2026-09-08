/**
 * The text match behind the New Tab page's search field, shared by everything that field finds:
 * the machine's instances and "Open new" actions, the "Start something" intents, and the templates.
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
