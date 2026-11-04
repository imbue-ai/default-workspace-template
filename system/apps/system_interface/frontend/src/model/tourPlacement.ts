/**
 * Where a tour card sits, as a pure function of what it points at (the new-onboarding spec, section 2.6): popped
 * out of the avatar with its bottom-left corner at the avatar's top-right; beside a target the shell draws (below,
 * above, right, or left, whichever has room) with an arrow; or at the top edge under the Imbue Studio chrome's
 * control, arrow up, when the card is about the chrome. Everything is clamped into the viewport.
 */

export interface Box {
  readonly left: number;
  readonly top: number;
  readonly width: number;
  readonly height: number;
}

export interface Size {
  readonly width: number;
  readonly height: number;
}

/** Which edge of the card the arrow sits on, pointing at the target; null for a card popped out of the avatar. */
export type ArrowSide = "top" | "bottom" | "left" | "right";

export interface TourPlacement {
  readonly left: number;
  readonly top: number;
  readonly arrow: { readonly side: ArrowSide; readonly offset: number } | null;
}

/** Space between the card and what it points at, and between the card and the viewport's edge. */
export const TOUR_GAP = 12;
export const TOUR_EDGE = 12;
/** How far in from the left a card about a left-hand chrome control sits (under the workspace name and its tabs). */
const CHROME_LEFT_INSET = 160;

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(value, max));
}

function clampLeft(left: number, card: Size, viewport: Size): number {
  return clamp(left, TOUR_EDGE, Math.max(TOUR_EDGE, viewport.width - card.width - TOUR_EDGE));
}

function clampTop(top: number, card: Size, viewport: Size): number {
  return clamp(top, TOUR_EDGE, Math.max(TOUR_EDGE, viewport.height - card.height - TOUR_EDGE));
}

/** Popped out of the avatar: the card's bottom-left corner at the avatar's top-right, no arrow. */
export function placeByAvatar(avatar: Box | null, card: Size, viewport: Size): TourPlacement {
  if (avatar === null) {
    return { left: TOUR_EDGE, top: clampTop(viewport.height - card.height - TOUR_EDGE, card, viewport), arrow: null };
  }
  return {
    left: clampLeft(avatar.left + avatar.width + TOUR_GAP, card, viewport),
    top: clampTop(avatar.top - card.height, card, viewport),
    arrow: null,
  };
}

/** Beside a target: below or above it (whichever has room, below first), else to its right or left. */
export function placeBeside(target: Box, card: Size, viewport: Size): TourPlacement {
  const centerX = target.left + target.width / 2;
  const centerY = target.top + target.height / 2;
  const fitsBelow = target.top + target.height + TOUR_GAP + card.height <= viewport.height - TOUR_EDGE;
  const fitsAbove = target.top - TOUR_GAP - card.height >= TOUR_EDGE;
  if (fitsBelow || fitsAbove) {
    const left = clampLeft(centerX - card.width / 2, card, viewport);
    const top = fitsBelow ? target.top + target.height + TOUR_GAP : target.top - TOUR_GAP - card.height;
    return {
      left,
      top,
      arrow: { side: fitsBelow ? "top" : "bottom", offset: clamp(centerX - left, 16, card.width - 16) },
    };
  }
  const top = clampTop(centerY - card.height / 2, card, viewport);
  const fitsRight = target.left + target.width + TOUR_GAP + card.width <= viewport.width - TOUR_EDGE;
  const left = fitsRight
    ? target.left + target.width + TOUR_GAP
    : clampLeft(target.left - TOUR_GAP - card.width, card, viewport);
  return {
    left,
    top,
    arrow: { side: fitsRight ? "left" : "right", offset: clamp(centerY - top, 16, card.height - 16) },
  };
}

/** Under the chrome: at the top edge, on the side the control is on, arrow up. */
export function placeUnderChrome(side: "left" | "right", card: Size, viewport: Size): TourPlacement {
  const left =
    side === "left"
      ? clampLeft(CHROME_LEFT_INSET, card, viewport)
      : clampLeft(viewport.width - card.width - TOUR_EDGE, card, viewport);
  return { left, top: TOUR_EDGE, arrow: { side: "top", offset: side === "left" ? 40 : card.width - 40 } };
}
