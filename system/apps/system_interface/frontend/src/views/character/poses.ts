/**
 * What the character does, per mood -- the layer between the workspace's
 * avatar status and the rig's verbs.
 *
 * Only the moods and moments the workspace can actually signal are mapped here,
 * and the rig below carries only the channels these drive. Richer behaviours --
 * a glance, a question -- are a matter of scoring the rig's verbs, and wait on
 * something in the workspace being able to ask for them.
 */
import type { BlobRig } from "./rig";

/** The moods the shell pushes (`avatar_status.mood`). */
export type CharacterMood = "idle" | "working";

/**
 * The resting lean, in radians, clockwise -- the character stands slightly
 * slanted to the right rather than square to the frame.
 *
 * Held on the tilt spring rather than baked into the silhouette, so anything
 * that wants it upright asks for a smaller tilt and springs there instead of
 * cutting. Subtler than the lean a turn takes (0.2): this one is a posture,
 * not a movement, and it is on screen the whole time.
 */
export const REST_TILT = 0.1;

/**
 * The lean while the user is here. Negative: the body's own long axis runs up
 * and to the right, so at zero rotation it still reads as leaning, and
 * standing it up means rotating back past zero against that diagonal.
 *
 * Tuned by eye against true vertical rather than derived -- the outline is too
 * round for its principal axis to mean anything. It reads as standing from
 * about -15; past roughly -25 it looks like a lean the other way, and the idle
 * wander moves the outline a couple of degrees either side of wherever it sits.
 */
export const ATTENDING_TILT = (-20 * Math.PI) / 180;

/**
 * Hold the posture for whether the user is here: drawn up while they are, and
 * slouched back when they leave. The whole of the response -- the user arrives
 * by clicking the character, and a body that jumps out from under the pointer
 * that just pressed it fights the press rather than answering it.
 *
 * Idempotent: the tilt is a spring target, so asking twice costs nothing, and
 * asking mid-motion lets the body travel to the new lean from wherever it is.
 */
export function posture(rig: BlobRig, isAttending: boolean): void {
  rig.tilt(isAttending ? ATTENDING_TILT : REST_TILT);
}

/**
 * Move the character to `mood`.
 *
 * Working is a condition, not an event: the surface goes unsettled and stays
 * that way for as long as there is work, pressing a shallow dent somewhere,
 * letting it out, and pressing somewhere else. Nothing marks the moment it
 * starts, because the signal behind this mood is "is any agent on the machine
 * running" -- it crosses at an instant the user has no reason to recognise. A
 * continuous state can be coarse and still read true; a gesture cannot.
 */
export function applyMood(rig: BlobRig, mood: CharacterMood, isAttending: boolean): void {
  rig.configure({ busy: mood === "working" ? 1 : 0 });
  posture(rig, isAttending);
}

/**
 * Press the character at `angle` (radians, from its centre): a dent where the
 * pointer landed, and a little give under the press.
 *
 * There is no drag counterpart: the character is a taskbar entry the user can
 * already drag to move, and two meanings for one gesture is one too many.
 */
export function press(rig: BlobRig, angle: number): void {
  rig.poke(angle, 1.1);
  rig.squish(0.22);
}

/** Let go of a press. */
export function releasePress(rig: BlobRig): void {
  rig.release();
}

/** How deep a hover dents, in radii -- under the working dents, so it reads as a flinch and not a blow. */
const HOVER_DENT_DEPTH = 0.025;
/** How far a hover shifts the body away from the pointer, in screen pixels whatever size it is drawn at. */
const HOVER_SHIFT_PX = 2;

/**
 * Lean away from a pointer resting at `angle` (radians from the centre, as
 * `press`): a slight dent where it is and the body a couple of pixels the other
 * way. `unitsPerPx` is the character's drawing scale, so the shift stays a
 * couple of pixels in a small entry and a large one alike. Held where it was
 * first asked for until `unhover`.
 */
export function hover(rig: BlobRig, angle: number, unitsPerPx: number): void {
  const away = HOVER_SHIFT_PX * unitsPerPx;
  rig.shy(angle, HOVER_DENT_DEPTH, -away * Math.cos(angle), -away * Math.sin(angle));
}

/** The pointer has gone; settle back. */
export function unhover(rig: BlobRig): void {
  rig.unshy();
}

/** Peak height of the arrival jump, in radii. */
const JUMP_HEIGHT = 0.95;
/** Crouch before launching, and the beat before pushing off. */
const JUMP_CROUCH = 0.6;
const JUMP_GATHER = 0.07;
/** Squash on touchdown. */
const JUMP_IMPACT = 1.8;

/**
 * Jump once: gather, launch, float, fall, land -- what the character does when
 * it has just been chosen, so the switch reads as someone turning up rather
 * than a picture being swapped.
 *
 * Only the launch and the touchdown are cued. Stretching up, easing off at the
 * apex and stretching again on the way down come from the hop velocity, so they
 * are in step with the arc. Both deliberate shape changes are impulses on the
 * impact channel, so the jump leaves the held posture alone.
 */
export function jump(rig: BlobRig): void {
  // Push off just past the deepest point of the crouch, so the rebound is still
  // unfolding as it leaves the ground. Without the crouch it reads as being
  // yanked upward rather than pushing off.
  rig.pop(JUMP_CROUCH);
  rig.after(JUMP_GATHER, () => {
    const air = rig.hop(JUMP_HEIGHT);
    rig.after(air, () => {
      // The impact arrives from underneath, so fold it and dent the bottom.
      rig.pop(JUMP_IMPACT);
      rig.poke(Math.PI / 2, 0.6);
    });
  });
}
