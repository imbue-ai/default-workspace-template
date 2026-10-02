/**
 * The character as a passive drawing: one settled frame of the rig, emitted as
 * a design the shell can serve like any other.
 *
 * The avatar chooser previews every design through the image route, and the
 * route serves passive SVG. The character cannot be drawn that way -- it is a
 * rig -- so it ships a still of its resting pose for the places that need a
 * picture, and the live component for the place that shows it off.
 *
 * Generated rather than drawn by hand, so the still is always the pose the rig
 * actually rests in. Regenerate it with:
 *
 *   npx esbuild --bundle --platform=node --format=esm \
 *     --loader:.svg=dataurl src/views/character/emitStill.ts --outfile=/tmp/emit.mjs \
 *     && node /tmp/emit.mjs > \
 *     ../imbue/system_interface/avatar/assets/imbue-character.svg
 */
import { REST_TILT } from "./poses";
import { createBlobRig } from "./rig";

/** The character's fill: imbue red. */
export const CHARACTER_COLOR = "#F50D00";

/** The grid every avatar design is drawn on. */
const DESIGN_SIZE = 100;
/** The rig's rest radius, in its own units. */
const R = 100;
/**
 * How much of the 100-unit grid the resting body fills.
 *
 * The bundled designs leave room for their motion and so does this: at 0.4 a
 * body of radius 100 lands 40 units from centre inside a half-grid of 50,
 * which is the same headroom the drawn designs keep for a hop.
 */
const FIT = 0.4;

/**
 * Simulated seconds the rig is run for before the frame is taken, and the step
 * they are run in.
 *
 * Long enough for the idle wander to have deformed the body, because that
 * wander is where the resting silhouette's blobbiness comes from: a rig
 * settled with its idle off relaxes to the bare oval underneath, which is the
 * shape the character is *built from* rather than the shape it ever holds. The
 * wander is driven by the config's seeded random, so this frame is the same
 * one every time it is generated.
 */
const SETTLE_STEP = 1 / 60;
const SETTLE_FRAMES = 200;

/** The character's resting pose as passive design markup. */
export function characterStillSvg(color: string = CHARACTER_COLOR): string {
  const rig = createBlobRig({ radius: R });
  rig.settle();
  rig.tilt(REST_TILT);
  for (let i = 0; i < SETTLE_FRAMES; i++) rig.step(SETTLE_STEP);
  const frame = rig.frame();
  const place = `translate(${DESIGN_SIZE / 2} ${DESIGN_SIZE / 2}) scale(${FIT})`;
  const body = frame.transform === "" ? place : `${place} ${frame.transform}`;
  return [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${DESIGN_SIZE} ${DESIGN_SIZE}">`,
    `<title>Imbue character</title>`,
    `<g transform="${body}"><path d="${frame.d}" fill="${color}"/></g>`,
    `</svg>`,
  ].join("");
}
