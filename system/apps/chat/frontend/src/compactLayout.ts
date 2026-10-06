/**
 * The chat app's phone layout (docs/system/blueprint/desktop-interface/plan-phone-interface.md, "The chat app"). On
 * a touchscreen it is on while the window is phone-sized either way round, by the rule the shell's phone layout
 * follows too (workspace-ui ``device_queries``), so a phone keeps it turned sideways and a tablet never gets it. Under
 * a mouse it is on while the window is at most ``COMPACT_MAX_WIDTH_PX`` wide.
 *
 * The root decides by its own size and tells every chat page it frames (``ChatPageEmbedApi.setCompact``), since a
 * page's frame is narrower than the root beside the desktop's list; a page visited directly, which no root frames,
 * decides by its own size.
 */

import { PHONE_SIZED_CONDITIONS, TOUCH_MEDIA_QUERY } from "@imbue/workspace-ui/src/device_queries";

export const COMPACT_MAX_WIDTH_PX = 500;

// A mouse or trackpad (fine), or no pointer at all (none): anything but a touchscreen.
const NOT_TOUCH_POINTERS = ["(pointer: fine)", "(pointer: none)"];

export const COMPACT_MEDIA_QUERY = [
  ...PHONE_SIZED_CONDITIONS.map((condition) => `${TOUCH_MEDIA_QUERY} and ${condition}`),
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
