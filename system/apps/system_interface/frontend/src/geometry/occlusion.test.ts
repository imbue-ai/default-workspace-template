import { describe, expect, it } from "vitest";
import type { PixelRect } from "./frames";
import { isPageCovered } from "./occlusion";
import type { FrontWindow } from "./occlusion";

const METRICS = { titleBarHeight: 36, windowRadius: 12 };
const BACKDROP = { width: 1000, height: 800 };
const FULL: PixelRect = { x: 0, y: 0, width: 1000, height: 800 };

function opaque(rect: PixelRect): FrontWindow {
  return { rect, isContentOpaque: true };
}

describe("isPageCovered", () => {
  it("covers a maximized window's page with another maximized window, rounded corners and all", () => {
    expect(isPageCovered(FULL, [opaque(FULL)], BACKDROP, METRICS)).toBe(true);
  });

  it("leaves the notches where two snapped halves meet over a maximized window", () => {
    const halves = [
      opaque({ x: 0, y: 0, width: 500, height: 800 }),
      opaque({ x: 500, y: 0, width: 500, height: 800 }),
    ];
    expect(isPageCovered(FULL, halves, BACKDROP, METRICS)).toBe(false);
  });

  it("covers a page only two front windows hide between them", () => {
    const page = { x: 100, y: 100, width: 400, height: 300 };
    const top = opaque({ x: 50, y: 50, width: 500, height: 260 });
    const bottom = opaque({ x: 50, y: 250, width: 500, height: 250 });
    expect(isPageCovered(page, [top], BACKDROP, METRICS)).toBe(false);
    expect(isPageCovered(page, [top, bottom], BACKDROP, METRICS)).toBe(true);
  });

  it("does not count the see-through content of a front window whose page is not painted", () => {
    expect(isPageCovered(FULL, [{ rect: FULL, isContentOpaque: false }], BACKDROP, METRICS)).toBe(false);
  });

  it("only needs the part of a page inside the backdrop covered", () => {
    const page = { x: 800, y: 100, width: 400, height: 300 };
    const front = opaque({ x: 780, y: 80, width: 240, height: 340 });
    expect(isPageCovered(page, [front], BACKDROP, METRICS)).toBe(true);
    expect(isPageCovered(page, [front], { width: 1300, height: 800 }, METRICS)).toBe(false);
  });

  it("counts a page whose content box lies wholly below the backdrop as covered", () => {
    expect(isPageCovered({ x: 100, y: 790, width: 400, height: 300 }, [], BACKDROP, METRICS)).toBe(true);
  });
});
