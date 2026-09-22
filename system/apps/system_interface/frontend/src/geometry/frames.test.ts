import { describe, expect, it } from "vitest";
import { frameForState, frameFromPixels, frameToPixels, isResizeEdge, movedRect, resizedRect } from "./frames";

const BACKDROP = { width: 1000, height: 800 };
const MINIMUM = { windowMinWidth: 320, windowMinHeight: 240 };
const MOVE_METRICS = { titleMinVisible: 120, titleBarHeight: 36 };

describe("frameForState", () => {
  it("answers the placement's own frame only when normal", () => {
    const own = { x: 0.1, y: 0.2, width: 0.3, height: 0.4 };
    expect(frameForState(own, "NORMAL")).toBe(own);
    expect(frameForState(own, "SNAPPED_LEFT")).toEqual({ x: 0, y: 0, width: 0.5, height: 1 });
    expect(frameForState(own, "SNAPPED_RIGHT")).toEqual({ x: 0.5, y: 0, width: 0.5, height: 1 });
    expect(frameForState(own, "MAXIMIZED")).toEqual({ x: 0, y: 0, width: 1, height: 1 });
  });
});

describe("pixels and fractions", () => {
  it("round-trip through the backdrop", () => {
    const frame = { x: 0.1, y: 0.25, width: 0.5, height: 0.5 };
    const rect = frameToPixels(frame, BACKDROP);
    expect(rect).toEqual({ x: 100, y: 200, width: 500, height: 400 });
    expect(frameFromPixels(rect, BACKDROP)).toEqual(frame);
  });

  it("clamps a rectangle that overhangs the backdrop", () => {
    expect(frameFromPixels({ x: 900, y: -50, width: 400, height: 400 }, BACKDROP)).toEqual({
      x: 0.6,
      y: 0,
      width: 0.4,
      height: 0.5,
    });
  });

  it("fills the square when the backdrop has no size yet", () => {
    expect(frameFromPixels({ x: 1, y: 1, width: 1, height: 1 }, { width: 0, height: 0 })).toEqual({
      x: 0,
      y: 0,
      width: 1,
      height: 1,
    });
  });
});

describe("resizedRect", () => {
  const start = { x: 100, y: 100, width: 400, height: 300 };

  it("moves only the grabbed edges", () => {
    expect(resizedRect(start, "e", { x: 50, y: 999 }, MINIMUM)).toEqual({ x: 100, y: 100, width: 450, height: 300 });
    expect(resizedRect(start, "s", { x: 999, y: 20 }, MINIMUM)).toEqual({ x: 100, y: 100, width: 400, height: 320 });
    expect(resizedRect(start, "nw", { x: -10, y: -20 }, MINIMUM)).toEqual({ x: 90, y: 80, width: 410, height: 320 });
    expect(resizedRect(start, "se", { x: 10, y: 20 }, MINIMUM)).toEqual({ x: 100, y: 100, width: 410, height: 320 });
  });

  it("holds the minimum size from the anchored edge", () => {
    expect(resizedRect(start, "w", { x: 300, y: 0 }, MINIMUM)).toEqual({ x: 180, y: 100, width: 320, height: 300 });
    expect(resizedRect(start, "n", { x: 0, y: 300 }, MINIMUM)).toEqual({ x: 100, y: 160, width: 400, height: 240 });
  });

  it("knows the eight edges and nothing else", () => {
    expect(isResizeEdge("ne")).toBe(true);
    expect(isResizeEdge("up")).toBe(false);
  });
});

describe("movedRect", () => {
  const start = { x: 100, y: 100, width: 400, height: 300 };

  it("follows the pointer", () => {
    expect(movedRect(start, { x: 30, y: -40 }, BACKDROP, MOVE_METRICS)).toEqual({
      x: 130,
      y: 60,
      width: 400,
      height: 300,
    });
  });

  it("keeps the minimum visible title inside on either side and never above the top", () => {
    expect(movedRect(start, { x: -900, y: -500 }, BACKDROP, MOVE_METRICS)).toEqual({
      x: -280,
      y: 0,
      width: 400,
      height: 300,
    });
    expect(movedRect(start, { x: 2000, y: 2000 }, BACKDROP, MOVE_METRICS)).toEqual({
      x: 880,
      y: 764,
      width: 400,
      height: 300,
    });
  });
});
