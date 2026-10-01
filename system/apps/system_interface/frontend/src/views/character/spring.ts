/**
 * A one-dimensional damped spring, integrated by hand.
 *
 * Everything the blob does is a spring settling toward a target. That is the
 * whole reason the rig is "easy to animate": you never author a curve, you set
 * a target (or throw an impulse at it) and the overshoot/ring-down that makes
 * the motion feel gooey falls out of the physics for free.
 *
 * Two ways to drive one:
 *   - `hold`  move `target`; the spring chases it and stays there.
 *   - `kick`  inject velocity; the spring rings and returns to `target`.
 */

export type Spring = {
  value: number;
  velocity: number;
  target: number;
  /** Angular-frequency-squared, in units of 1/s². Higher = snappier. */
  stiffness: number;
  /** Damping ratio: <1 rings, 1 is critical (no overshoot), >1 is sluggish. */
  ratio: number;
};

export function makeSpring(value: number, stiffness: number, ratio: number): Spring {
  return { value, velocity: 0, target: value, stiffness, ratio };
}

/**
 * Semi-implicit Euler. Stable at the sub-steps the rig feeds it (1/240 s) even
 * for the stiff high-order surface modes, and cheap enough that a blob's
 * handful of springs costs nothing.
 */
export function stepSpring(s: Spring, dt: number): void {
  const damping = 2 * s.ratio * Math.sqrt(s.stiffness);
  const accel = s.stiffness * (s.target - s.value) - damping * s.velocity;
  s.velocity += accel * dt;
  s.value += s.velocity * dt;
}

/**
 * Impulse sized so the first swing peaks near `amplitude`, whatever the
 * stiffness. An undamped spring given velocity v swings to v/ω, so scaling by
 * ω = sqrt(stiffness) makes the gesture's *size* the thing you ask for and
 * leaves stiffness to control only its *speed*.
 *
 * Without this, stiffening the surface silently shrinks every wobble, and no
 * amount of tuning gets you a rig whose gestures survive a settings change.
 */
export function kick(s: Spring, amplitude: number): void {
  s.velocity += amplitude * Math.sqrt(s.stiffness);
}

/** Snap a spring to rest at `value`, killing any motion. */
export function settleSpring(s: Spring, value: number): void {
  s.value = value;
  s.target = value;
  s.velocity = 0;
}
