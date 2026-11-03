import { describe, expect, it } from "vitest";
import type { PixelRect } from "./frames";
import { isPageCovered } from "./occlusion";
import type { DrawnWindowRect, FrontWindow } from "./occlusion";

const METRICS = { titleBarHeight: 36, windowRadius: 12 };
const BACKDROP = { width: 1000, height: 800 };
const FULL: PixelRect = { x: 0, y: 0, width: 1000, height: 800 };

function opaque(rect: PixelRect): FrontWindow {
  return { rect, hasTitleBar: true, isContentOpaque: true };
}

/** A window under test with a title bar, drawn at ``rect``. */
function titled(rect: PixelRect): DrawnWindowRect {
  return { rect, hasTitleBar: true };
}

describe("isPageCovered", () => {
  it("covers a maximized window's page with another maximized window, rounded corners and all", () => {
    expect(isPageCovered(titled(FULL), [opaque(FULL)], BACKDROP, METRICS)).toBe(true);
  });

  it("leaves the notches where two snapped halves meet over a maximized window", () => {
    const halves = [
      opaque({ x: 0, y: 0, width: 500, height: 800 }),
      opaque({ x: 500, y: 0, width: 500, height: 800 }),
    ];
    expect(isPageCovered(titled(FULL), halves, BACKDROP, METRICS)).toBe(false);
  });

  it("covers a page only two front windows hide between them", () => {
    const page = { x: 100, y: 100, width: 400, height: 300 };
    const top = opaque({ x: 50, y: 50, width: 500, height: 260 });
    const bottom = opaque({ x: 50, y: 250, width: 500, height: 250 });
    expect(isPageCovered(titled(page), [top], BACKDROP, METRICS)).toBe(false);
    expect(isPageCovered(titled(page), [top, bottom], BACKDROP, METRICS)).toBe(true);
  });

  it("does not count the see-through content of a front window whose page is not painted", () => {
    expect(
      isPageCovered(titled(FULL), [{ rect: FULL, hasTitleBar: true, isContentOpaque: false }], BACKDROP, METRICS),
    ).toBe(false);
  });

  it("only needs the part of a page inside the backdrop covered", () => {
    const page = { x: 800, y: 100, width: 400, height: 300 };
    const front = opaque({ x: 780, y: 80, width: 240, height: 340 });
    expect(isPageCovered(titled(page), [front], BACKDROP, METRICS)).toBe(true);
    expect(isPageCovered(titled(page), [front], { width: 1300, height: 800 }, METRICS)).toBe(false);
  });

  it("gives a fullscreen window, which has no title bar, no title bar's height of page or of cover", () => {
    // Covering all but the strip a title bar would take: a fullscreen page shows there.
    const belowTop = opaque({ x: 0, y: 10, width: 1000, height: 790 });
    expect(isPageCovered(titled(FULL), [belowTop], BACKDROP, METRICS)).toBe(true);
    expect(isPageCovered({ rect: FULL, hasTitleBar: false }, [belowTop], BACKDROP, METRICS)).toBe(false);
    // A page only a see-through window's title bar would hide shows through a fullscreen one.
    const underTopStrip = titled({ x: 100, y: -30, width: 400, height: 60 });
    const seeThrough = { rect: FULL, hasTitleBar: true, isContentOpaque: false };
    expect(isPageCovered(underTopStrip, [seeThrough], BACKDROP, METRICS)).toBe(true);
    expect(isPageCovered(underTopStrip, [{ ...seeThrough, hasTitleBar: false }], BACKDROP, METRICS)).toBe(false);
  });

  it("gives a fullscreen window square corners, which a rounded one in front leaves showing and a fullscreen one covers", () => {
    const fullscreenPage = { rect: FULL, hasTitleBar: false };
    // Rounded windows over the top corners, and a maximized one over the rest: only the bottom corners stay open.
    const roundedFront = [
      opaque({ x: -50, y: 0, width: 100, height: 100 }),
      opaque({ x: 950, y: 0, width: 100, height: 100 }),
      opaque(FULL),
    ];
    expect(isPageCovered(titled(FULL), roundedFront, BACKDROP, METRICS)).toBe(true);
    expect(isPageCovered(fullscreenPage, roundedFront, BACKDROP, METRICS)).toBe(false);
    const fullscreenFront = { rect: FULL, hasTitleBar: false, isContentOpaque: true };
    expect(isPageCovered(fullscreenPage, [fullscreenFront], BACKDROP, METRICS)).toBe(true);
  });

  it("counts a page whose content box lies wholly below the backdrop as covered", () => {
    expect(isPageCovered(titled({ x: 100, y: 790, width: 400, height: 300 }), [], BACKDROP, METRICS)).toBe(true);
  });
});
