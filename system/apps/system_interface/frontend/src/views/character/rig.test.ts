import { describe, expect, it } from "vitest";
import { type BlobRig, createBlobRig } from "./rig";

/** A rig with the ambient motion off, so a test measures only the gesture. */
function still(config = {}): BlobRig {
  return createBlobRig({ radius: 100, idle: 0, ...config });
}

function run(rig: BlobRig, seconds: number, each?: (rig: BlobRig) => void): void {
  for (let i = 0; i < Math.round(seconds * 60); i++) {
    rig.step(1 / 60);
    each?.(rig);
  }
}

/** Largest distance from the center reached by any anchor, over `seconds`. */
function peakRadius(rig: BlobRig, seconds: number): number {
  let peak = 0;
  run(rig, seconds, (r) => {
    for (const a of r.frame().anchors) peak = Math.max(peak, Math.hypot(a.x, a.y));
  });
  return peak;
}

describe("squash", () => {
  it("never flattens into a needle, however hard it is held", () => {
    // Regression: with full area preservation the spring's undershoot on a
    // held squish drove the width to several times the radius.
    const rig = still();
    rig.squish(1);
    run(rig, 2, (r) => {
      const [sx, sy] = r.frame().scale;
      expect(sy).toBeGreaterThanOrEqual(0.42);
      expect(sy).toBeLessThanOrEqual(1.85);
      expect(sx / sy).toBeLessThan(5);
    });
  });

  it("comes back to rest after release", () => {
    const rig = still();
    rig.squish(1);
    run(rig, 0.3);
    rig.release();
    run(rig, 3);
    const [sx, sy] = rig.frame().scale;
    expect(sy).toBeCloseTo(1, 2);
    expect(sx).toBeCloseTo(1, 2);
  });

  it("overshoots on the way back, rather than easing in flat", () => {
    const rig = still();
    rig.squish(1);
    run(rig, 0.4);
    rig.release();
    let tallest = 0;
    run(rig, 1, (r) => {
      tallest = Math.max(tallest, r.frame().scale[1]);
    });
    expect(tallest).toBeGreaterThan(1.02);
  });
});

describe("poke", () => {
  it("deforms by the same amount at any stiffness", () => {
    // Regression: impulses used to be raw velocity, so the wobble silently
    // shrank as the surface stiffened and no tuning survived a settings change.
    const soft = peakRadius(
      (() => {
        const r = still({ surfaceStiffness: 60 });
        r.poke(0, 1);
        return r;
      })(),
      1,
    );
    const hard = peakRadius(
      (() => {
        const r = still({ surfaceStiffness: 400 });
        r.poke(0, 1);
        return r;
      })(),
      1,
    );
    expect(Math.abs(soft - hard) / soft).toBeLessThan(0.12);
  });

  it("scales with force", () => {
    const light = peakRadius(
      (() => {
        const r = still();
        r.poke(0, 0.4);
        return r;
      })(),
      1,
    );
    const heavy = peakRadius(
      (() => {
        const r = still();
        r.poke(0, 1.4);
        return r;
      })(),
      1,
    );
    expect(heavy).toBeGreaterThan(light + 8);
  });

  it("settles back to the rest silhouette", () => {
    // Springs approach rest asymptotically, so this is a tolerance on the
    // anchors rather than string equality on the path.
    const rig = still();
    const before = rig.frame().anchors;
    rig.poke(1.2, 1.5);
    run(rig, 6);
    const after = rig.frame().anchors;
    for (let i = 0; i < before.length; i++) {
      expect(after[i].x).toBeCloseTo(before[i].x, 1);
      expect(after[i].y).toBeCloseTo(before[i].y, 1);
    }
  });
});

describe("step", () => {
  it("survives a huge delta without producing NaN", () => {
    // What a backgrounded tab hands you on the frame it comes back.
    const rig = still();
    rig.poke(0, 1);
    rig.step(9);
    run(rig, 1);
    expect(rig.frame().d).not.toMatch(/NaN|Infinity/);
  });
});

describe("settle", () => {
  it("drops every channel back to the rest pose at once", () => {
    const rig = still();
    const before = rig.frame();
    rig.poke(0.5, 2);
    rig.squish(1);
    rig.tilt(0.4);
    run(rig, 0.2);
    rig.settle();
    const after = rig.frame();
    expect(after.d).toBe(before.d);
    expect(after.transform).toBe(before.transform);
  });
});

/**
 * The head the resting shape is derived from -- the measured dot with its
 * underside rebuilt -- restated here so the test pins the shape independently
 * of the rig.
 */
const HEAD: Array<[k: number, cos: number, sin: number]> = [
  [1, 0.0024, 0.0019],
  [2, -0.012, -0.0772],
  [3, -0.0054, -0.0169],
  [4, -0.0008, 0.0048],
  [5, -0.004, 0.0012],
  [6, 0.0046, -0.0015],
];

/** The head's radius at an angle, as a fraction of R. */
function headRadius(theta: number): number {
  let r = 1;
  for (const [k, a, b] of HEAD) r += a * Math.cos(k * theta) + b * Math.sin(k * theta);
  return r;
}

describe("the head the resting shape comes from", () => {
  /** Signed curvature of the polar outline; negative means a concave dent. */
  function curvature(theta: number): number {
    let r = 1;
    let d1 = 0;
    let d2 = 0;
    for (const [k, a, b] of HEAD) {
      r += a * Math.cos(k * theta) + b * Math.sin(k * theta);
      d1 += -k * a * Math.sin(k * theta) + k * b * Math.cos(k * theta);
      d2 += -k * k * a * Math.cos(k * theta) - k * k * b * Math.sin(k * theta);
    }
    return (r * r + 2 * d1 * d1 - r * d2) / (r * r + d1 * d1) ** 1.5;
  }

  it("is convex everywhere -- no dent where the letter's stem used to start", () => {
    // The raw measurement dipped to -0.62 around 50 degrees.
    for (let i = 0; i < 720; i++) {
      expect(curvature((2 * Math.PI * i) / 720)).toBeGreaterThan(0);
    }
  });

  it("does not hang below an ellipse at the bottom", () => {
    // The raw fit extrapolated 1.125 here; an ellipse through the same k=2
    // would be 1.026, and anything much past that reads as a bulge.
    expect(headRadius(Math.PI / 2)).toBeLessThan(1.04);
  });

  it("leaves no stem artefact in the resting pose either", () => {
    // The resting profile is this head relaxed, so the outline the character
    // actually holds has to come out clean underneath too.
    const rig = still({ irregularity: 1 });
    run(rig, 1);
    const anchors = rig.frame().anchors;
    const n = anchors.length;
    for (let i = 0; i < n; i++) {
      const prev = anchors[(i - 1 + n) % n];
      const next = anchors[(i + 1) % n];
      const cross =
        (anchors[i].x - prev.x) * (next.y - anchors[i].y) - (anchors[i].y - prev.y) * (next.x - anchors[i].x);
      // One consistent turn direction the whole way round means convex.
      expect(cross).toBeGreaterThan(0);
    }
  });
});

describe("the shadow on the floor", () => {
  it("fades as the idle float carries the body off the floor", () => {
    const rig = createBlobRig({ radius: 100 });
    /** How far the frame has carried the body up or down, in user units. */
    const travel = (r: BlobRig): number => Number(/translate\(0 (-?[\d.]+)\)/.exec(r.frame().transform)?.[1] ?? 0);
    let highest = { y: 0, weight: 1 };
    let lowest = { y: 0, weight: 1 };
    run(rig, 5, (r) => {
      const y = travel(r);
      if (y < highest.y) highest = { y, weight: r.frame().shadow.weight };
      if (y > lowest.y) lowest = { y, weight: r.frame().shadow.weight };
    });
    // The float is small, so this is the only thing keeping the pool alive.
    expect(highest.y).toBeLessThan(-2);
    expect(lowest.y).toBeGreaterThan(2);
    expect(highest.weight).toBeLessThan(lowest.weight);
  });

  it("widens with the body, so a squash spreads the pool", () => {
    const rig = still();
    const rest = rig.frame().shadow.spread;
    rig.squish(1);
    run(rig, 0.5);
    expect(rig.frame().shadow.spread).toBeGreaterThan(rest * 1.1);
  });
});

describe("the resting silhouette", () => {
  /**
   * Harmonic `k` of r(θ) read off the anchors: how strong it is, as a fraction
   * of the mean radius, and the angle its long axis points along.
   */
  function harmonic(rig: BlobRig, k: number): { amp: number; axis: number } {
    const anchors = rig.frame().anchors;
    const n = anchors.length;
    const r = anchors.map((a) => Math.hypot(a.x, a.y));
    const mean = r.reduce((sum, v) => sum + v, 0) / n;
    let c = 0;
    let s = 0;
    anchors.forEach((a, i) => {
      const theta = Math.atan2(a.y, a.x);
      c += (r[i] / mean) * Math.cos(k * theta);
      s += (r[i] / mean) * Math.sin(k * theta);
    });
    c = (2 * c) / n;
    s = (2 * s) / n;
    return { amp: Math.hypot(c, s), axis: (Math.atan2(s, c) * 180) / Math.PI / k };
  }

  /** How far apart two axes of a k-fold mode are, in degrees. A k-fold axis
   *  repeats every 360/k, so the raw difference overstates it. */
  function axisGap(a: number, b: number, k: number): number {
    const period = 360 / k;
    const d = Math.abs(a - b) % period;
    return Math.min(d, period - d);
  }

  it("rests as the mark's own ellipse, neither rounded off nor exaggerated", () => {
    const rig = still();
    const r = rig.frame().anchors.map((a) => Math.hypot(a.x, a.y));
    // The mark's own k=2 (see `OVAL_GAIN`).
    expect(harmonic(rig, 2).amp).toBeCloseTo(0.078, 2);
    // Still a leaning body rather than a ball, and short of the flattening
    // that sets in at A = 0.2.
    expect(Math.max(...r) / Math.min(...r)).toBeGreaterThan(1.1);
    expect(harmonic(rig, 2).amp).toBeLessThan(0.18);
  });

  it("keeps its long axis put while the lumps wander off it", () => {
    // The lumps drift so none of them sits in one place looking like a fixed
    // dent; the oval is held out of that, being which way up the body is.
    const rig = createBlobRig({ radius: 100 });
    run(rig, 1);
    const oval = harmonic(rig, 2).axis;
    const lumps = harmonic(rig, 3).axis;
    // Forty seconds is about half a turn of the drift.
    run(rig, 40);
    expect(axisGap(harmonic(rig, 2).axis, oval, 2)).toBeLessThan(3);
    expect(axisGap(harmonic(rig, 3).axis, lumps, 3)).toBeGreaterThan(20);
  });
});

describe("working dents", () => {
  /** Distance from the centre at each anchor. */
  function radii(rig: BlobRig): number[] {
    return rig.frame().anchors.map((a) => Math.hypot(a.x, a.y));
  }

  /** How far the outline is from a circle, as a max/min radius ratio. */
  function outOfRound(rig: BlobRig): number {
    const r = radii(rig);
    return Math.max(...r) / Math.min(...r);
  }

  /** The rest profile switched off, so only the dents move the outline. */
  function busy(): BlobRig {
    return still({ irregularity: 0, idle: 1, busy: 1, seed: 3 });
  }

  it("presses the surface and holds it, for as long as the work lasts", () => {
    const rig = busy();
    let worst = 1;
    run(rig, 14, (r) => {
      worst = Math.max(worst, outOfRound(r));
    });
    expect(worst).toBeGreaterThan(1.04);
  });

  it("lets the dent out again when the work stops", () => {
    const rig = busy();
    run(rig, 3);
    rig.configure({ busy: 0 });
    // Long enough for a held dent to spring out.
    run(rig, 3);
    expect(outOfRound(rig)).toBeCloseTo(1, 3);
  });

  it("does not press the surface on its own while there is no work", () => {
    // With `busy` at 0 these are the only ambient thing that presses the
    // outline rather than scaling or moving it -- breathing is uniform and
    // the float is a translation, so with the rest profile switched off too a
    // bare circle is what fourteen seconds of idling should leave.
    //
    // Make them press while idle and this is meant to fail: flip it to
    // `expect(worst).toBeGreaterThan(1.04)`.
    const rig = still({ irregularity: 0, idle: 1, seed: 3 });
    let worst = 1;
    run(rig, 14, (r) => {
      worst = Math.max(worst, outOfRound(r));
    });
    expect(worst).toBeCloseTo(1, 3);
  });

  it("holds perfectly still when idle is off", () => {
    const rig = still({ irregularity: 0 });
    run(rig, 14, (r) => {
      expect(outOfRound(r)).toBeCloseTo(1, 3);
    });
  });
});
