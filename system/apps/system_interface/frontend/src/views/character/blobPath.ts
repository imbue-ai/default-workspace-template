/**
 * The blob's geometry: a closed cubic-Bézier loop through N anchors on a ring.
 *
 * Shape comes from *surface modes*, not from moving anchors by hand. Each
 * anchor's radius is `1 + Σ aₖ·cos(kθ) + bₖ·sin(kθ)`, so the whole silhouette is
 * described by a handful of numbers:
 *
 *   k=2  the ellipse mode  -- stretch along an axis (the big, slow wobble)
 *   k=3  the triangle mode -- a lopsided three-lobed sway
 *   k=4  the square mode   -- the fast ripple that fades first
 *
 * That is how a real droplet oscillates, which is why poking one mode and
 * letting it ring reads as "jelly" rather than as "shape being tweened". It
 * also keeps the animation surface tiny: 6 scalars instead of 24 coordinates.
 *
 * The imbue mark's own blobs are built the same way -- very few anchors, handles
 * pulled long past where a circle would put them. `goo` is that overshoot: 1 is
 * a true circle, and above 1 the curve bulges between anchors the way the logo's
 * counters do.
 */

/** One surface harmonic, as its cos/sin pair so it can point any direction. */
export type BlobMode = { k: number; cos: number; sin: number };

export type BlobShape = {
  /** Nominal radii. Different rx/ry scales the finished curve -- exactly, since
   *  an affine map of a Bézier is the same map applied to its control points. */
  rx: number;
  ry: number;
  /** Anchors around the ring. Must exceed 2× the highest mode k, or that mode
   *  aliases into a lower one. 20 comfortably carries modes up to 9. */
  points?: number;
  modes?: readonly BlobMode[];
  /** Handle-length multiplier. 1 = circle, >1 = the logo's gooey overshoot. */
  goo?: number;
};

/** Radius floor, so a violent poke dents the blob instead of inverting it. */
const RADIUS_FLOOR = 0.3;

export type BlobAnchor = {
  /** Anchor point. */
  x: number;
  y: number;
  /** Outgoing control point (toward the next anchor). */
  outX: number;
  outY: number;
  /** Incoming control point (from the previous anchor). */
  inX: number;
  inY: number;
};

/** The modal radius at one angle, before any rx/ry scaling. 1 is the rest ring. */
export function unitRadiusAt(theta: number, modes: readonly BlobMode[] = []): number {
  let r = 1;
  for (const m of modes) r += m.cos * Math.cos(m.k * theta) + m.sin * Math.sin(m.k * theta);
  return r;
}

/**
 * Anchors plus their two control points, in user units.
 *
 * Handles run along the chord between an anchor's neighbours (the tangent of a
 * circle through the three) and are scaled by that anchor's own radius, so a
 * bulge gets long handles and bulges harder while a dent pulls tight -- the
 * metaball neck you see where the imbue "i" meets its dot.
 */
export function blobAnchors(shape: BlobShape): BlobAnchor[] {
  const { rx, ry, points = 12, modes = [], goo = 1 } = shape;
  const n = Math.max(3, Math.round(points));

  // Exact circular-arc handle length for an n-gon of unit radius: the classic
  // (4/3)·tan(π/2n). With goo = 1 and no modes the result is a true circle.
  const handle = goo * (4 / 3) * Math.tan(Math.PI / (2 * n));

  const px: number[] = [];
  const py: number[] = [];
  const pr: number[] = [];
  for (let i = 0; i < n; i++) {
    const theta = (2 * Math.PI * i) / n;
    const r = Math.max(RADIUS_FLOOR, unitRadiusAt(theta, modes));
    px.push(r * Math.cos(theta));
    py.push(r * Math.sin(theta));
    pr.push(r);
  }

  const anchors: BlobAnchor[] = [];
  for (let i = 0; i < n; i++) {
    const prev = (i - 1 + n) % n;
    const next = (i + 1) % n;
    let tx = px[next] - px[prev];
    let ty = py[next] - py[prev];
    const len = Math.hypot(tx, ty) || 1;
    tx /= len;
    ty /= len;
    const h = handle * pr[i];
    anchors.push({
      x: px[i] * rx,
      y: py[i] * ry,
      outX: (px[i] + tx * h) * rx,
      outY: (py[i] + ty * h) * ry,
      inX: (px[i] - tx * h) * rx,
      inY: (py[i] - ty * h) * ry,
    });
  }
  return anchors;
}

/** Trim float noise out of the path data -- it is rebuilt every frame. */
function f(n: number): string {
  return (Math.round(n * 100) / 100).toString();
}

/** One closed subpath through a ring of anchors, using their own handles. */
export function pathFromAnchors(a: readonly BlobAnchor[]): string {
  let d = `M${f(a[0].x)} ${f(a[0].y)}`;
  for (let i = 0; i < a.length; i++) {
    const to = a[(i + 1) % a.length];
    d += `C${f(a[i].outX)} ${f(a[i].outY)} ${f(to.inX)} ${f(to.inY)} ${f(to.x)} ${f(to.y)}`;
  }
  return `${d}Z`;
}
