/**
 * The two decisions behind renaming a tab, kept apart from the two places that
 * offer it: the double-click editor on the tab itself, and the same gesture on
 * a row of the project settings dialog's "In this project" list.
 *
 * Both go through the same rename in the end, so they have to agree on what a
 * typed title becomes and on which objects can carry one at all. Neither answer
 * needs a DOM, a panel or a project, which is why they live here.
 */

/** Longest title kept. Far more than the 220px tab ceiling can show -- the
 *  strip fades the overflow rather than ellipsizing it -- but short enough that
 *  a pasted paragraph does not end up in the view's saved layout. */
export const MAX_TAB_TITLE_LENGTH = 120;

/**
 * What a typed title becomes, or null when it is not a title at all.
 *
 * Whitespace is collapsed to single spaces and trimmed, so a stray newline from
 * a paste cannot put a line break in a tab strip. A title that is empty once
 * trimmed is *null* rather than an empty string: there is no such thing as a
 * nameless tab, and a tab with nothing to click on would be worse than the name
 * it replaced. Callers treat null as "leave the name alone", which is the same
 * outcome as Escape.
 */
export function normalizeTabTitle(raw: string): string | null {
  const collapsed = raw.replace(/\s+/g, " ").trim();
  if (collapsed === "") return null;
  // Trimmed again in case the cut landed on the space between two words.
  return collapsed.slice(0, MAX_TAB_TITLE_LENGTH).trimEnd();
}

/**
 * Why this object cannot be renamed right now, or null when it can.
 *
 * A title belongs to the panel showing the object and is saved with that panel
 * in the view's layout, so a backgrounded member -- still running, just not
 * docked -- has nowhere to keep one. Rather than take a name and quietly drop
 * it, the affordance is refused, and this sentence is the tooltip that says so.
 */
export function tabRenameBlockedReason(row: { isOpen: boolean }): string | null {
  if (!row.isOpen) {
    return "Open this to rename it. A name is kept with the tab showing it, so there is nowhere to put one while it is closed.";
  }
  return null;
}
