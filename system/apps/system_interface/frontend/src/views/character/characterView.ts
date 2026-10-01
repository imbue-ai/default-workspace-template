/**
 * Runs a rig and writes the result straight to the DOM.
 *
 * Deliberately outside Mithril's redraw: the path changes every frame, and
 * redrawing a component 60 times a second to hand it a new string is pure
 * overhead. The component renders once, the loop mutates a few attributes.
 * Mithril still owns everything that changes at human speed -- colour and
 * size.
 */
import type { BlobRig } from "./rig";

export interface CharacterElements {
  /** The body group, which carries the frame's transform. */
  group: SVGGElement;
  /** The one filled path the character is drawn as. */
  path: SVGPathElement;
  /** The pool on the floor. Outside the body group, since a shadow does not
   *  rise or tip over with what casts it. */
  shadow: SVGEllipseElement | null;
}

/** Writes the rig's current frame into `elements`. */
export function createPainter(rig: BlobRig, elements: CharacterElements): () => void {
  const { group, path, shadow } = elements;

  return () => {
    const f = rig.frame();
    path.setAttribute("d", f.d);
    group.setAttribute("transform", f.transform);
    if (shadow) {
      // Scaled about its own centre, which is why the floor's depth is a
      // transform on the group outside this one rather than a cy here.
      shadow.setAttribute("transform", `scale(${f.shadow.spread.toFixed(3)})`);
      shadow.setAttribute("opacity", f.shadow.weight.toFixed(3));
    }
  };
}

/**
 * The longest step the rig is ever asked to take, in seconds.
 *
 * A backgrounded tab stops getting frames and hands back one enormous gap on
 * its return; fed to the springs whole, that is not a slow frame but a
 * teleport, and the body arrives inside out. Capping it means a character that
 * was away comes back mid-motion rather than wrong.
 */
const MAX_STEP = 1 / 15;

/** Steps `rig` on every frame and paints it, until the returned function is
 *  called. */
export function driveCharacter(rig: BlobRig, elements: CharacterElements): () => void {
  const paint = createPainter(rig, elements);
  let handle = 0;
  let last = performance.now();

  // The frame's own timestamp is ignored in favour of the clock it is taken
  // from: they are the same clock in a browser, and this way the loop does not
  // depend on getting one.
  const tick = (): void => {
    const now = performance.now();
    const dt = Math.min(Math.max((now - last) / 1000, 0), MAX_STEP);
    last = now;
    rig.step(dt);
    paint();
    handle = requestAnimationFrame(tick);
  };

  handle = requestAnimationFrame(tick);
  return () => cancelAnimationFrame(handle);
}

/** Simulated seconds run to bring a just-settled rig onto its held pose, and
 *  the step they are run in. `settle` zeroes every spring, so a pose applied
 *  after it is a target the springs still have to travel to; painting before
 *  they arrive would draw the character square rather than in its posture. */
const REST_CONVERGE_STEP = 1 / 60;
const REST_CONVERGE_FRAMES = 120;

/** Paints one settled frame in `pose` and never animates -- the reduced-motion
 *  path. The CSS designs answer that preference by holding every pose still; a
 *  rig has to be told, since its motion is not an animation the sheet can stop. */
export function paintAtRest(rig: BlobRig, elements: CharacterElements, pose?: (rig: BlobRig) => void): void {
  rig.settle();
  pose?.(rig);
  for (let i = 0; i < REST_CONVERGE_FRAMES; i++) rig.step(REST_CONVERGE_STEP);
  createPainter(rig, elements)();
}
