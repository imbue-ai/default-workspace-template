/**
 * The query-string edit a boot-time parameter is removed with once read: the deep link (``deepLinks.ts``) is
 * taken out of the URL, leaving every other parameter (solo mode's among them) as it was.
 */

/** The query string with ``names`` removed (other parameters kept), "" when none remain. */
export function withoutParams(search: string, names: readonly string[]): string {
  const params = new URLSearchParams(search);
  for (const name of names) params.delete(name);
  const remaining = params.toString();
  return remaining === "" ? "" : `?${remaining}`;
}
