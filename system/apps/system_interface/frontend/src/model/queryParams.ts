/**
 * The one query-string edit the boot-time parameters share: the deep link (``deepLinks.ts``) and solo mode
 * (``soloMode.ts``) are each read once and then removed from the URL, leaving every other parameter as it was.
 */

/** The query string with ``names`` removed (other parameters kept), "" when none remain. */
export function withoutParams(search: string, names: readonly string[]): string {
  const params = new URLSearchParams(search);
  for (const name of names) params.delete(name);
  const remaining = params.toString();
  return remaining === "" ? "" : `?${remaining}`;
}
