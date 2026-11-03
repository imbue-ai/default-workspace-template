/**
 * The letterbox a viewed desktop is drawn in (the multiplayer spec, section 8.3): the owner's backdrop, scaled
 * down to fit the viewer's and centred, so the viewer sees the desktop as its owner sees it. Nothing here reads
 * the DOM.
 */

import type { PixelPoint, PixelSize } from "./frames";

/** Where and how large the owner's backdrop is drawn in the viewer's: its size (the box the frames are fractions
 *  of), the one scale applied to it, and the offset that centres the scaled box. */
export interface Letterbox {
  readonly size: PixelSize;
  readonly scale: number;
  readonly offset: PixelPoint;
}

/** The letterbox that fits ``theirs`` (the owner's backdrop) in ``ours``: scaled down to fit, never up, and
 *  centred; a degenerate size on either side is drawn unscaled at the corner. */
export function letterboxFor(theirs: PixelSize, ours: PixelSize): Letterbox {
  if (theirs.width <= 0 || theirs.height <= 0 || ours.width <= 0 || ours.height <= 0) {
    return { size: theirs, scale: 1, offset: { x: 0, y: 0 } };
  }
  const scale = Math.min(ours.width / theirs.width, ours.height / theirs.height, 1);
  return {
    size: theirs,
    scale,
    offset: { x: (ours.width - theirs.width * scale) / 2, y: (ours.height - theirs.height * scale) / 2 },
  };
}
