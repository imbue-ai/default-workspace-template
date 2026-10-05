/**
 * The chat app's phone layout (docs/system/blueprint/desktop-interface/plan-phone-interface.md, "The chat app"). On
 * a touchscreen it is on while the window is phone-sized either way round, by the shell's own rule for its phone
 * layout (system_interface ``PHONE_MEDIA_QUERY``): a short side at most ``PHONE_MAX_SHORT_SIDE_PX`` and a long side
 * at most ``PHONE_MAX_LONG_SIDE_PX``, so a phone keeps it turned sideways and a tablet never gets it. Under a mouse
 * it is on while the window is at most ``COMPACT_MAX_WIDTH_PX`` wide.
 *
 * The root decides by its own size and tells every chat page it frames (``ChatPageEmbedApi.setCompact``), since a
 * page's frame is narrower than the root beside the desktop's list; a page visited directly, which no root frames,
 * decides by its own size.
 */

export const COMPACT_MAX_WIDTH_PX = 500;
export const PHONE_MAX_SHORT_SIDE_PX = 500;
export const PHONE_MAX_LONG_SIDE_PX = 1000;

/** A touchscreen, whose finger wants the phone layout's larger targets; a narrow window under a mouse keeps the
 *  desktop's. The shell decides the same way (system_interface ``TOUCH_MEDIA_QUERY``). */
export const TOUCH_MEDIA_QUERY = "(pointer: coarse)";

// A mouse or trackpad (fine), or no pointer at all (none): anything but a touchscreen.
const NOT_TOUCH_POINTERS = ["(pointer: fine)", "(pointer: none)"];

export const COMPACT_MEDIA_QUERY = [
  `${TOUCH_MEDIA_QUERY} and (max-width: ${PHONE_MAX_SHORT_SIDE_PX}px) and (max-height: ${PHONE_MAX_LONG_SIDE_PX}px)`,
  `${TOUCH_MEDIA_QUERY} and (max-height: ${PHONE_MAX_SHORT_SIDE_PX}px) and (max-width: ${PHONE_MAX_LONG_SIDE_PX}px)`,
  ...NOT_TOUCH_POINTERS.map((pointer) => `${pointer} and (max-width: ${COMPACT_MAX_WIDTH_PX}px)`),
].join(", ");

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
