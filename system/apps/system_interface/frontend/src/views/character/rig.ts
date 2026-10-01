/**
 * The blob rig: every way the character can move, as springs you poke.
 *
 * The rig is plain TypeScript with no framework in it -- construct one, call
 * `step(dt)` each frame, read `frame()`. Three independent channels:
 *
 *   surface   the modal wobble (see blobPath.ts) -- six springs that ring
 *   body      squash & stretch, one spring, area-preserving
 *   tilt      the lean, which is the whole of the character's posture
 *
 * Every channel takes the same two kinds of input, which is the whole API:
 *
 *   hold      `squish(0.6)`  set a target; it stays until `release()`
 *   impulse   `poke(angle)`  throw velocity at it; it rings and comes home
 *
 * So a pose is never a keyframe track -- it is a hold or a poke, and the
 * physics supplies the follow-through in between.
 */
import { type BlobAnchor, type BlobMode, blobAnchors, pathFromAnchors } from "./blobPath";
import { kick, makeSpring, type Spring, settleSpring, stepSpring } from "./spring";

export type BlobRigConfig = {
  /** Rest radius in user units. The frame is built around (0, 0). */
  radius: number;
  /** Anchors on the ring. Keep above 2× the largest mode number; 20 carries
   *  the k=5 detail in the rest profile with room to spare. */
  points: number;
  /** Which surface harmonics exist. 2 = ellipse, 3 = triangle, 4 = square. */
  modeNumbers: readonly number[];
  /** Bézier handle overshoot: 1 is a circle, ~1.1 is the logo's goo. */
  goo: number;
  /** Ring-down rate of the k=2 surface mode; higher modes scale up from it. */
  surfaceStiffness: number;
  /** Surface damping ratio. Low numbers ring for a long time. */
  surfaceRatio: number;
  /** Squash/stretch spring. */
  bodyStiffness: number;
  bodyRatio: number;
  /** Tilt spring. */
  tiltStiffness: number;
  tiltRatio: number;
  /** Scales the rest profile -- the oval and the lumps together. 0 is a bare
   *  circle, 1 is the resting shape as tuned (see `IDLE_PROFILE`), above 1
   *  exaggerates it. */
  irregularity: number;
  /** Breathing, lump drift and float, 0..1. 0 holds perfectly still. */
  idle: number;
  /** How much a squash widens the blob. 1 conserves area exactly; a little
   *  under reads as jelly rather than as rubber, which is why the default
   *  is not 1. 0 squashes with no widening at all. */
  bulge: number;
  /**
   * How unsettled the surface is while the character is busy: 0 is settled,
   * 1 the full working wobble.
   */
  busy: number;
  /** Seeds the idle wander and the directions the working dents pick. */
  seed: number;
};

export const DEFAULT_CONFIG: BlobRigConfig = {
  radius: 100,
  points: 20,
  modeNumbers: [2, 3, 4],
  goo: 1.08,
  surfaceStiffness: 70,
  surfaceRatio: 0.4,
  bodyStiffness: 70,
  bodyRatio: 0.4,
  tiltStiffness: 120,
  tiltRatio: 0.42,
  irregularity: 1,
  idle: 1.25,
  bulge: 0.8,
  busy: 0,
  seed: 7,
};

export type BlobFrame = {
  /** The body outline, as one closed subpath. */
  d: string;
  /** Group transform: float and tilt, applied outside the path. */
  transform: string;
  /** Body anchors and handles -- the outline before it is written as a path. */
  anchors: BlobAnchor[];
  /** Current squash, as (horizontal, vertical) scale. */
  scale: [number, number];
  /**
   * The cast shadow on the surface below: how far it spreads, and how dark it
   * is -- 1 being the shadow of a resting blob.
   *
   * Numbers rather than a shape, because how dark a shadow is at a given height
   * is character motion and belongs here, while what a shadow *looks* like --
   * its softness, its ink, how far under the body the floor is -- is rendering
   * and belongs to the component drawing it.
   */
  shadow: { spread: number; weight: number };
};

/*
 * Provenance -- the raw least-squares fit of the "i" dot in the imbue mark. Its
 * outline was traced, `r(θ)` sampled from the centroid, and modes fitted over
 * the 87% of the boundary not hidden where the dot meets its stem:
 *
 *   k=1  +0.0005  +0.0067        k=4  +0.0245  +0.0056
 *   k=2  -0.0260  -0.0799        k=5  -0.0039  +0.0215
 *   k=3  -0.0032  -0.0395        k=6  -0.0066  -0.0019
 *
 * What those numbers say, if the shape is ever retuned by hand: it is a
 * **tilted ellipse**, k=2 carrying most of it at 0.084 with its long axis
 * running up-right. Not an egg -- k=1 measured 0.007, so there is no offset
 * term at all, and adding one moves the shape away from the mark, not toward
 * it. Nothing derives from these directly; `HEAD` below is this fit
 * with the stem taken off, and the resting silhouette comes from that.
 */

/**
 * The head, with the letter's stem taken off.
 *
 * The character is a head and nothing else; the dot in the mark is a head *on a
 * stem*. The raw fit carries two artefacts of that stem. A real,
 * measured concave dent at 40–60° where the stem begins -- curvature there runs
 * to -0.62 -- and a bottom that swells to 1.125 R, which is pure extrapolation,
 * since that arc is the one hidden behind the neck in the source drawing.
 * Together they read as a bulge hanging off the lower right.
 *
 * So the lower arc is blended back to the shape's *own* k=2 ellipse (full
 * ellipse over 25–120°, smoothstep ramps out to 0° and 160°) and the result
 * re-fitted to six modes. The outline is convex everywhere afterwards -- worst
 * curvature +0.57 -- its bottom sits at 1.027 R instead of 1.125, and the upper
 * two-thirds, the part that was actually measured and that carries the
 * character, moves by under 0.8% of R.
 */
const HEAD: readonly BlobMode[] = [
  { k: 1, cos: 0.0024, sin: 0.0019 },
  { k: 2, cos: -0.012, sin: -0.0772 },
  { k: 3, cos: -0.0054, sin: -0.0169 },
  { k: 4, cos: -0.0008, sin: 0.0048 },
  { k: 5, cos: -0.004, sin: 0.0012 },
  { k: 6, cos: 0.0046, sin: -0.0015 },
];

/**
 * How much of the head's k≥3 detail the resting pose keeps.
 *
 * Idling is the character at its least deliberate, so its lopsidedness relaxes
 * toward a plain oval -- but only part way. At 0 it looks drawn by a compass.
 */
const RELAXED_DETAIL = 0.35;

/**
 * How far past the mark's own ellipse the body leans.
 *
 * Written as r(θ) = R(1 + A·cos2(θ-φ)), an amplitude A gives a long-to-short
 * axis ratio of (1+A)/(1-A). At unity the body carries the mark's own k=2,
 * 0.078, a ratio of 1.17 -- the resting shape is the mark's ellipse and
 * nothing added to it, since the posture and the working dents are what say
 * the character is alive.
 *
 * Exaggerating it has a hard ceiling: `(1-A)(1-5A)` is the curvature at the
 * ends of the *short* axis, so the long sides flatten as A climbs, go straight
 * at A = 0.2 and turn inward past it -- a ratio of 1.5, with the flattening
 * plainly visible by 1.44.
 */
const OVAL_GAIN = 1;

/**
 * The idle silhouette: the same head, relaxed.
 *
 * Derived from `HEAD`, not from the raw fit -- the character is a head and
 * nothing else, so the stem artefacts have no business in its resting pose.
 *
 * k=1 and k=6 are dropped: both measure under 0.005 here, which is below what
 * the outline shows.
 *
 * The oval is the mark's own and is left at that (see `OVAL_GAIN`). It is not
 * asked to carry the character by itself: the posture says whether the user is
 * here, and the surface dents say whether there is work (see
 * `BUSY_DENT_DEPTH`).
 */
const IDLE_PROFILE: readonly BlobMode[] = HEAD.filter((m) => m.k >= 2 && m.k <= 5).map((m) => {
  const g = m.k === 2 ? OVAL_GAIN : RELAXED_DETAIL;
  return { k: m.k, cos: m.cos * g, sin: m.sin * g };
});

/**
 * How the shadow answers height: it spreads and fades as the body leaves the
 * floor, per radius of lift.
 *
 * Tight and dark in contact, wide and faint in the air. That is what a real
 * penumbra does, and it is the cue every bouncing-ball animation leans on -- for
 * a character with no legs it is the only thing that says the floor is there at
 * all. Keyed off the same lift the body uses, so the idle float reads on the
 * floor for free.
 */
const SHADOW_SPREAD = 0.22;
const SHADOW_FADE = 0.55;

/**
 * Working dents: how deep one presses, how long it holds, and the gap between.
 *
 * The surface takes a shallow press somewhere, holds it, lets it out, and
 * presses somewhere else -- so a busy body is one that is never quite settled,
 * rather than one playing an animation. Deep enough to see, which a resting
 * flutter must not be and this must: being visibly unsettled is the whole of
 * looking busy.
 *
 * The depth has a ceiling, and it is the same one the oval has: at twice this
 * the outline notches inward on the deeper presses, and at three times it is a
 * kidney bean rather than a body.
 */
const BUSY_DENT_DEPTH = 0.09;
const BUSY_DENT_HOLD = 0.5;
const BUSY_DENT_GAP: readonly [number, number] = [0.12, 0.45];

/** Peak surface displacement, in radii, for a force-1 poke. */
const POKE_DEPTH = 0.17;
/** Hard stops on the squash spring, as a fraction of rest height. */
const SQUASH_LIMIT: readonly [number, number] = [0.42, 1.85];
const SUBSTEP = 1 / 240;

/** mulberry32 -- a seeded PRNG, so the working dents replay identically. */
function makeRandom(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Surface modes stiffen with order the way a droplet's do: ωₖ² ∝ k(k²-1). */
function modeStiffness(base: number, k: number): number {
  return (base * (k * (k * k - 1))) / 6;
}

export type BlobRig = ReturnType<typeof createBlobRig>;

export function createBlobRig(overrides: Partial<BlobRigConfig> = {}) {
  let config: BlobRigConfig = { ...DEFAULT_CONFIG, ...overrides };
  let random = makeRandom(config.seed);

  // Each surface mode is a pair of springs -- a cos and a sin amplitude -- so the
  // wobble can point in any direction and even rotate as it rings down.
  let surface: Array<{ k: number; cos: Spring; sin: Spring }> = [];
  function buildSurface() {
    surface = config.modeNumbers.map((k) => ({
      k,
      cos: makeSpring(0, modeStiffness(config.surfaceStiffness, k), config.surfaceRatio),
      sin: makeSpring(0, modeStiffness(config.surfaceStiffness, k), config.surfaceRatio),
    }));
  }
  buildSurface();

  /** Vertical scale. 1 is rest; below is squashed, above is stretched. */
  const body = makeSpring(1, config.bodyStiffness, config.bodyRatio);
  const tiltS = makeSpring(0, config.tiltStiffness, config.tiltRatio);

  let clock = 0;

  // Whether a working dent is currently held, and when the next change is due.
  let denting = false;
  let dentAt = 2;

  // Gestures.

  /**
   * Dent the surface at `angle` (radians, 0 = right, clockwise in screen
   * space). Low modes take most of the energy, which is what an impact does to
   * a real drop -- the blob lurches away from the hit, then rings.
   */
  function poke(angle: number, force = 1): void {
    for (const m of surface) {
      const share = (force * POKE_DEPTH) / (m.k - 1);
      kick(m.cos, -share * Math.cos(m.k * angle));
      kick(m.sin, -share * Math.sin(m.k * angle));
    }
  }

  /**
   * Press a dent into the surface at `angle` and hold it there.
   *
   * The held twin of `poke`: the same falloff across the modes, but as targets
   * rather than as velocity, so the surface arrives at the dent and stays,
   * instead of ringing through it and coming back. `smooth` lets it out.
   */
  function dent(angle: number, depth = 0.1): void {
    for (const m of surface) {
      const share = depth / (m.k - 1);
      m.cos.target = -share * Math.cos(m.k * angle);
      m.sin.target = -share * Math.sin(m.k * angle);
    }
  }

  /** Let every held dent out; the surface springs back to round. */
  function smooth(): void {
    for (const m of surface) {
      m.cos.target = 0;
      m.sin.target = 0;
    }
  }

  /**
   * Hold a squash. `amount` 0..1; 1 targets 60% height, which with the default
   * damping overshoots to roughly 46% before settling -- chosen so a full-force
   * squash rings freely instead of slamming into SQUASH_LIMIT.
   */
  function squish(amount = 1): void {
    body.target = 1 - 0.4 * Math.max(0, Math.min(1, amount));
  }

  /** Let the body spring back to its rest height. */
  function release(): void {
    body.target = 1;
  }

  /** Hold a tilt, in radians. */
  function tilt(radians: number): void {
    tiltS.target = radians;
  }

  /** Kill all motion and return to the rest pose immediately. */
  function settle(): void {
    for (const m of surface) {
      settleSpring(m.cos, 0);
      settleSpring(m.sin, 0);
    }
    settleSpring(body, 1);
    settleSpring(tiltS, 0);
    denting = false;
    dentAt = clock + 2;
  }

  /** Somewhere in `gap`, in seconds. */
  function nextAt(gap: readonly [number, number]): number {
    return clock + gap[0] + random() * (gap[1] - gap[0]);
  }

  // Simulation.

  let carry = 0;
  function step(dt: number): void {
    // Cap the catch-up after a background tab or a slow frame, or the springs
    // get handed a huge dt and explode on the first frame back.
    carry += Math.min(dt, 0.1);
    while (carry >= SUBSTEP) {
      for (const m of surface) {
        stepSpring(m.cos, SUBSTEP);
        stepSpring(m.sin, SUBSTEP);
      }
      stepSpring(body, SUBSTEP);
      // The squash spring is allowed to overshoot -- that bounce is the point --
      // but not so far that `bulge` blows the width up into a needle. Hitting
      // a stop kills the velocity heading into it, the way a real one would.
      if (body.value < SQUASH_LIMIT[0]) {
        body.value = SQUASH_LIMIT[0];
        body.velocity = Math.max(0, body.velocity);
      } else if (body.value > SQUASH_LIMIT[1]) {
        body.value = SQUASH_LIMIT[1];
        body.velocity = Math.min(0, body.velocity);
      }
      stepSpring(tiltS, SUBSTEP);
      clock += SUBSTEP;
      carry -= SUBSTEP;
    }

    // Holding perfectly still means perfectly still, and so does not being
    // busy: either way any held dent is let out, and the cycle is armed to
    // start on the first frame of the next busy spell rather than part way in.
    if (config.idle <= 0 || config.busy <= 0) {
      if (denting) {
        smooth();
        denting = false;
      }
      dentAt = clock;
    } else if (clock >= dentAt) {
      if (denting) {
        smooth();
        denting = false;
        dentAt = nextAt(BUSY_DENT_GAP);
      } else {
        dent(random() * Math.PI * 2, BUSY_DENT_DEPTH * config.busy);
        denting = true;
        dentAt = clock + BUSY_DENT_HOLD;
      }
    }
  }

  // Readout.

  function frame(): BlobFrame {
    const idle = config.idle;

    // Rest lumps rotate slowly, so the blob's irregularity migrates around it
    // instead of sitting in one place looking like a fixed dent.
    //
    // The oval itself is held out of that. k=2 is not a lump, it is which way
    // up the character is, and drifting it would roll the whole body over about
    // once a minute and a half.
    const drift = clock * 0.06 * idle;
    const scale = config.irregularity;
    const modes: BlobMode[] = IDLE_PROFILE.map((m) => {
      const a = m.k === 2 ? 0 : m.k * drift;
      return {
        k: m.k,
        cos: scale * (m.cos * Math.cos(a) - m.sin * Math.sin(a)),
        sin: scale * (m.cos * Math.sin(a) + m.sin * Math.cos(a)),
      };
    });
    for (const m of surface) modes.push({ k: m.k, cos: m.cos.value, sin: m.sin.value });

    const breath = 1 + 0.018 * idle * Math.sin((clock * Math.PI * 2) / 3.4);
    // `step` already holds the squash inside its stops.
    const sy = body.value;
    const sx = sy ** -config.bulge;
    const rx = config.radius * breath * sx;
    const ry = config.radius * breath * sy;

    const anchors = blobAnchors({ rx, ry, points: config.points, modes, goo: config.goo });

    const float = 0.02 * config.radius * idle * Math.sin((clock * Math.PI * 2) / 4.7);
    const deg = (tiltS.value * 180) / Math.PI;
    // Height above the resting spot, in radii -- which is the float and nothing
    // else, so the pool on the floor only ever breathes with it.
    const lift = -float / config.radius;

    return {
      d: pathFromAnchors(anchors),
      transform: `translate(0 ${float.toFixed(2)}) rotate(${deg.toFixed(2)})`,
      anchors,
      scale: [sx, sy],
      shadow: {
        // Tracks the body's own width, so a squash spreads the pool on top of
        // what the height does.
        spread: (rx / config.radius) * (1 + SHADOW_SPREAD * lift),
        weight: 1 - SHADOW_FADE * lift,
      },
    };
  }

  /**
   * Change the rig live. Anything that alters the spring set (mode numbers,
   * stiffness, damping) rebuilds the surface springs, which drops whatever
   * wobble was mid-flight.
   */
  function configure(next: Partial<BlobRigConfig>): void {
    const rebuild =
      (next.modeNumbers !== undefined && next.modeNumbers !== config.modeNumbers) ||
      (next.surfaceStiffness !== undefined && next.surfaceStiffness !== config.surfaceStiffness) ||
      (next.surfaceRatio !== undefined && next.surfaceRatio !== config.surfaceRatio);
    const reseed = next.seed !== undefined && next.seed !== config.seed;
    config = { ...config, ...next };
    if (rebuild) buildSurface();
    if (reseed) random = makeRandom(config.seed);
    body.stiffness = config.bodyStiffness;
    body.ratio = config.bodyRatio;
    tiltS.stiffness = config.tiltStiffness;
    tiltS.ratio = config.tiltRatio;
  }

  return {
    step,
    frame,
    configure,
    get config() {
      return config;
    },
    poke,
    squish,
    release,
    tilt,
    settle,
  };
}
