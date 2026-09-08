/**
 * How a New Tab offer answers the pointer. The page has two of them -- a template card and a
 * "Start something" tile -- and both float off the page on hover while exactly one thing inside
 * them grows: the picture. On a card that is its drawing, which fills the card; on a tile it is
 * the glyph, which is the only picture a tile has. The tile itself holds still, because a tile is
 * mostly text, and moving a paragraph under the pointer is harder to read than moving an icon.
 *
 * 300ms is slower than the product's other feedback (``--dur-slow`` is 200ms) because the others
 * are state changes that should keep up with the pointer, while this one is a surface moving, and
 * at 200ms that reads as a flinch rather than a lift. It is written as a plain duration rather
 * than a token because it is this gesture's own timing, not the shared one.
 *
 * Only scale and shadow animate: neither takes part in layout, so nothing here can reflow the rail
 * or the grid around it. Everything shares ``HOVER_LIFT_TRANSITION``, so the pieces can only ever
 * be timed alike; what differs is which element moves and what drives it.
 *
 * The transition names ``scale``, not ``transform``: a ``scale-*`` utility sets the standalone
 * ``scale`` property rather than writing into ``transform``, so a transition over ``transform``
 * matches nothing and the growth lands in one frame while the shadow eases in around it.
 *
 * The ``group`` variants need their hover target to carry Tailwind's ``group`` class -- the card's
 * button, the tile's button -- so the whole offer is the target and its text lifts the picture too.
 */

export const HOVER_LIFT_TRANSITION = "transition-[scale,box-shadow] duration-300 ease-out";

/** A template card's drawing: it grows a touch and floats, driven by the card around it. */
export const HOVER_LIFT_GROUP = `${HOVER_LIFT_TRANSITION} group-hover:scale-[1.02] group-hover:shadow-overlay`;

/** A tile: it floats without moving, so the text under the pointer stays where it was. */
export const HOVER_SHADOW_SELF = `${HOVER_LIFT_TRANSITION} hover:shadow-overlay`;

/**
 * The glyph inside such a tile: the one thing that grows. The step is much larger than a card's
 * (a 24px glyph moving 2% would not be visible at all), and it grows from its left edge so it
 * stays lined up with the title and sentence under it instead of drifting into the tile's padding.
 */
export const HOVER_GLYPH_GROUP = `${HOVER_LIFT_TRANSITION} origin-left group-hover:scale-[1.15]`;
