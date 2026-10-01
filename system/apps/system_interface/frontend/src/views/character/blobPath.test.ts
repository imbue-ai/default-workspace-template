import { describe, expect, it } from "vitest";
import { type BlobAnchor, blobAnchors, pathFromAnchors, unitRadiusAt } from "./blobPath";

/** Point on the cubic through four control points, at parameter t. */
function cubicAt(a: BlobAnchor, b: BlobAnchor, t: number): { x: number; y: number } {
  const u = 1 - t;
  const w = [u ** 3, 3 * u * u * t, 3 * u * t * t, t ** 3];
  return {
    x: w[0] * a.x + w[1] * a.outX + w[2] * b.inX + w[3] * b.x,
    y: w[0] * a.y + w[1] * a.outY + w[2] * b.inY + w[3] * b.y,
  };
}

describe("unitRadiusAt", () => {
  it("is 1 everywhere with no modes", () => {
    expect(unitRadiusAt(0)).toBe(1);
    expect(unitRadiusAt(2.3, [])).toBe(1);
  });

  it("puts a mode's peak where its cos/sin phase points", () => {
    // A pure cos k=2 mode peaks at theta = 0 and pi, dips at +/- pi/2.
    const modes = [{ k: 2, cos: 0.2, sin: 0 }];
    expect(unitRadiusAt(0, modes)).toBeCloseTo(1.2, 10);
    expect(unitRadiusAt(Math.PI, modes)).toBeCloseTo(1.2, 10);
    expect(unitRadiusAt(Math.PI / 2, modes)).toBeCloseTo(0.8, 10);
  });
});

describe("blobAnchors", () => {
  it("draws a true circle at rest -- goo 1, no modes", () => {
    const anchors = blobAnchors({ rx: 100, ry: 100, points: 12, goo: 1 });
    // Sample along every segment, not just at the anchors, since the anchors
    // are on the circle by construction and would pass trivially.
    for (let i = 0; i < anchors.length; i++) {
      const next = anchors[(i + 1) % anchors.length];
      for (const t of [0.15, 0.35, 0.5, 0.75]) {
        const p = cubicAt(anchors[i], next, t);
        expect(Math.hypot(p.x, p.y)).toBeCloseTo(100, 0.5);
      }
    }
  });

  it("scales rx and ry exactly, because the map is affine", () => {
    const modes = [{ k: 3, cos: 0.18, sin: -0.07 }];
    const unit = blobAnchors({ rx: 1, ry: 1, modes, points: 10 });
    const scaled = blobAnchors({ rx: 40, ry: 90, modes, points: 10 });
    for (let i = 0; i < unit.length; i++) {
      expect(scaled[i].x).toBeCloseTo(unit[i].x * 40, 9);
      expect(scaled[i].y).toBeCloseTo(unit[i].y * 90, 9);
      expect(scaled[i].outX).toBeCloseTo(unit[i].outX * 40, 9);
      expect(scaled[i].outY).toBeCloseTo(unit[i].outY * 90, 9);
    }
  });

  it("keeps handles collinear with the anchor, so the curve stays smooth", () => {
    const anchors = blobAnchors({
      rx: 100,
      ry: 60,
      points: 9,
      modes: [
        { k: 2, cos: 0.3, sin: 0.1 },
        { k: 4, cos: -0.2, sin: 0.25 },
      ],
    });
    for (const a of anchors) {
      const cross = (a.x - a.inX) * (a.outY - a.y) - (a.y - a.inY) * (a.outX - a.x);
      expect(Math.abs(cross)).toBeLessThan(1e-6);
    }
  });

  it("floors the radius so a violent poke dents instead of inverting", () => {
    // A mode this large would drive the radius negative around theta = pi/2.
    const anchors = blobAnchors({
      rx: 100,
      ry: 100,
      points: 16,
      modes: [{ k: 2, cos: -1.6, sin: 0 }],
    });
    for (const a of anchors) expect(Math.hypot(a.x, a.y)).toBeGreaterThanOrEqual(29.9);
  });
});

describe("pathFromAnchors", () => {
  it("emits one closed subpath with a curve per anchor", () => {
    const d = pathFromAnchors(blobAnchors({ rx: 10, ry: 10, points: 7 }));
    expect(d.startsWith("M")).toBe(true);
    expect(d.endsWith("Z")).toBe(true);
    expect(d.match(/C/g)).toHaveLength(7);
    expect(d).not.toMatch(/NaN|Infinity/);
  });
});
