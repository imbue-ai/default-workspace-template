/**
 * The chat app's phone layout (docs/system/blueprint/desktop-interface/plan-phone-interface.md, "The chat app"): on
 * while the chat root is at most ``COMPACT_MAX_WIDTH_PX`` wide, whether that is a phone or a narrow window on a
 * desktop.
 *
 * The root decides by its own width and tells every chat page it frames (``ChatPageEmbedApi.setCompact``), since a
 * page's frame is narrower than the root beside the desktop's list; a page visited directly, which no root frames,
 * decides by its own width.
 */

export const COMPACT_MAX_WIDTH_PX = 700;

export const COMPACT_MEDIA_QUERY = `(max-width: ${COMPACT_MAX_WIDTH_PX}px)`;

// What the framing root last said, or null for a page no root has told.
let compactFromRoot: boolean | null = null;

/** Whether the page draws the phone layout. */
export function isCompactLayout(): boolean {
  return compactFromRoot ?? window.matchMedia(COMPACT_MEDIA_QUERY).matches;
}

/** The framing root's layout, which the page follows from here on. */
export function setCompactFromRoot(isCompact: boolean): void {
  compactFromRoot = isCompact;
}
