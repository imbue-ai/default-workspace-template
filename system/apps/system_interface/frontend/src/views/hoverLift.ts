/**
 * The shared hover lift for the New Tab page's two offers, a template card and a "Start something"
 * tile. Everything here shares ``HOVER_LIFT_TRANSITION`` so the pieces cannot drift out of time.
 *
 * The transition names ``scale``, not ``transform``: a ``scale-*`` utility sets the standalone
 * ``scale`` property, so a transition over ``transform`` matches nothing and the growth lands in
 * one frame. A unit test pins the pairing.
 *
 * 300ms is this gesture's own timing and deliberately not ``--dur-slow`` (200ms), which is for
 * state changes that keep up with the pointer. Only scale and shadow animate, so nothing here can
 * reflow the rail or grid. The ``group-*`` variants need their hover target to carry Tailwind's
 * ``group``.
 */

export const HOVER_LIFT_TRANSITION = "transition-[scale,box-shadow] duration-300 ease-out";

/** A template card's drawing: it grows a touch and floats, driven by the card around it. */
export const HOVER_LIFT_GROUP = `${HOVER_LIFT_TRANSITION} group-hover:scale-[1.02] group-hover:shadow-overlay`;

/** A tile: it floats without moving, so the text under the pointer stays where it was. */
export const HOVER_SHADOW_SELF = `${HOVER_LIFT_TRANSITION} hover:shadow-overlay`;

/**
 * The glyph inside such a tile: the one thing that grows. Its step is much larger than a card's
 * because 2% of 24px would not be visible, and ``origin-left`` keeps it lined up with the text
 * under it.
 */
export const HOVER_GLYPH_GROUP = `${HOVER_LIFT_TRANSITION} origin-left group-hover:scale-[1.15]`;
