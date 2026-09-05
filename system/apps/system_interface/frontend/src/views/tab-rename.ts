/**
 * What a typed title becomes before it is filed as an object's name.
 *
 * Every rename path -- the tab's own double-click editor and the rail row's
 * editor alike -- hands the app's rename route (through the shell's relay) a
 * title normalized here first. Needs no DOM, panel or project, which is why it
 * lives apart from them.
 */

/** Longest title kept. Far more than the 220px tab ceiling can show -- the
 *  strip fades the overflow rather than ellipsizing it -- but short enough that
 *  a pasted paragraph does not end up as an instance's title. */
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
