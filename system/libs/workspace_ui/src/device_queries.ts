/**
 * The media queries that tell a phone from a desktop, shared by every app that draws a phone layout so they all
 * switch together (plan-phone-interface.md).
 *
 * A phone is a viewport phone-sized whichever way round: a short side at most ``PHONE_MAX_SHORT_SIDE_PX`` and a
 * long side at most ``PHONE_MAX_LONG_SIDE_PX``. Rotating a phone keeps it; no tablet is one, and neither is a desktop
 * window that is short but wide. A touchscreen is told apart separately, by its primary pointer.
 */

export const PHONE_MAX_SHORT_SIDE_PX = 500;
export const PHONE_MAX_LONG_SIDE_PX = 1000;

/** The phone-sized viewport, as the two conditions either of which is a phone: upright, then turned sideways. Kept
 *  apart so a caller can combine each with another condition, which a media query list cannot do as a whole. */
export const PHONE_SIZED_CONDITIONS: readonly string[] = [
  `(max-width: ${PHONE_MAX_SHORT_SIDE_PX}px) and (max-height: ${PHONE_MAX_LONG_SIDE_PX}px)`,
  `(max-height: ${PHONE_MAX_SHORT_SIDE_PX}px) and (max-width: ${PHONE_MAX_LONG_SIDE_PX}px)`,
];

export const PHONE_MEDIA_QUERY = PHONE_SIZED_CONDITIONS.join(", ");

/** A touchscreen: a finger is the primary pointer. */
export const TOUCH_MEDIA_QUERY = "(pointer: coarse)";
