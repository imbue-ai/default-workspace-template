/**
 * The two decisions behind renaming an object, kept apart from the two places
 * that offer it: the double-click editor on the tab itself, and the same
 * gesture on a row of the project settings dialog's "In this project" list.
 *
 * Both go through the same rename in the end, so they have to agree on what a
 * typed title becomes and on which rows can be renamed at all. Neither answer
 * needs a DOM, a panel or a project, which is why they live here.
 */

/** Longest title kept. Far more than the 220px tab ceiling can show -- the
 *  strip fades the overflow rather than ellipsizing it -- but short enough that
 *  a pasted paragraph does not end up in the machine's title store. Matches the
 *  backend's ``MAX_MEMBER_TITLE_LENGTH``, which rejects anything longer. */
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
 * A name is filed by REF and belongs to the object rather than to the panel
 * showing it (see models/MemberTitles), so *being open is no longer a
 * condition*: a backgrounded member -- still running, just not docked -- is
 * renameable like any other, because the name has somewhere to live either way.
 * Nothing a view lists is refused on what it is.
 *
 * What is left is the settings dialog's own staging. A row already marked for
 * removal is struck through and on its way out of that list, so it is not
 * offered a name in the same visit; the removal is undone with one click, and
 * the rename is there again. This sentence is the tooltip that says so.
 */
export function tabRenameBlockedReason(row: { isStagedForRemoval: boolean }): string | null {
  if (row.isStagedForRemoval) {
    return "This is being removed from the project on save. Undo the removal to rename it.";
  }
  return null;
}
