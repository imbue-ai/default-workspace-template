import { describe, expect, it } from "vitest";
import { letterboxFor } from "./letterbox";

describe("letterboxFor", () => {
  it("scales a larger backdrop down to fit on its tighter side and centres it on the other", () => {
    const box = letterboxFor({ width: 2000, height: 1000 }, { width: 1000, height: 800 });
    expect(box.size).toEqual({ width: 2000, height: 1000 });
    expect(box.scale).toBe(0.5);
    expect(box.offset).toEqual({ x: 0, y: 150 });
  });

  it("never scales a smaller backdrop up, only centres it", () => {
    const box = letterboxFor({ width: 600, height: 400 }, { width: 1000, height: 800 });
    expect(box.scale).toBe(1);
    expect(box.offset).toEqual({ x: 200, y: 200 });
  });

  it("draws an equal backdrop as it is", () => {
    const box = letterboxFor({ width: 1000, height: 800 }, { width: 1000, height: 800 });
    expect(box).toEqual({ size: { width: 1000, height: 800 }, scale: 1, offset: { x: 0, y: 0 } });
  });

  it("draws a backdrop unscaled at the corner before either side has a size", () => {
    expect(letterboxFor({ width: 1200, height: 900 }, { width: 0, height: 0 })).toEqual({
      size: { width: 1200, height: 900 },
      scale: 1,
      offset: { x: 0, y: 0 },
    });
  });
});
