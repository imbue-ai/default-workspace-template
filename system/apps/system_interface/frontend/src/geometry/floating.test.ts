import { describe, expect, it } from "vitest";
import { themeMetricsRecord } from "../testing/records";
import { defaultFloatingPosition, floatingEntryRect, floatingPositionFromPixels } from "./floating";

const METRICS = themeMetricsRecord();
const BACKDROP = { width: 1000, height: 800 };

describe("floating entries", () => {
  it("default to the bottom-right corner inset by the tokens", () => {
    const position = defaultFloatingPosition(BACKDROP, METRICS);
    expect(position.x).toBeCloseTo((1000 - 16 - 56) / 1000, 6);
    expect(position.y).toBeCloseTo((800 - 12 - 56) / 800, 6);
    expect(floatingEntryRect(position, BACKDROP, METRICS)).toEqual({ x: 928, y: 732, width: 56, height: 56 });
    expect(defaultFloatingPosition({ width: 0, height: 0 }, METRICS)).toEqual({ x: 0, y: 0 });
  });

  it("clamp a box so it stays wholly inside the backdrop, at render and when stored", () => {
    expect(floatingEntryRect({ x: 0.99, y: 0.99 }, BACKDROP, METRICS)).toEqual({
      x: 944,
      y: 744,
      width: 56,
      height: 56,
    });
    expect(floatingEntryRect({ x: 0, y: 0 }, BACKDROP, METRICS)).toEqual({ x: 0, y: 0, width: 56, height: 56 });
    expect(floatingPositionFromPixels({ x: -30, y: 790 }, BACKDROP, METRICS)).toEqual({ x: 0, y: 744 / 800 });
    expect(floatingPositionFromPixels({ x: 100, y: 200 }, BACKDROP, METRICS)).toEqual({ x: 0.1, y: 0.25 });
    // A backdrop smaller than the box pins the box to the origin rather than off it.
    expect(floatingEntryRect({ x: 0.5, y: 0.5 }, { width: 40, height: 40 }, METRICS)).toEqual({
      x: 0,
      y: 0,
      width: 56,
      height: 56,
    });
  });
});
