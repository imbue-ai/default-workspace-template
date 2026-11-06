// @vitest-environment jsdom
import "../../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IMBUE_CHARACTER_DESIGN_ID, ImbueCharacter, type ImbueCharacterAttrs } from "./ImbueCharacter";

/** jsdom has no matching media, and the component asks about reduced motion
 *  at mount; each test says which answer it wants. */
function setReducedMotion(isReduced: boolean): void {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: isReduced,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

/** Let the rAF loop run: the polyfill schedules on timers, and the rig reads a
 *  real clock, so both have to move. */
async function runFrames(count: number): Promise<void> {
  for (let i = 0; i < count; i++) {
    await vi.advanceTimersByTimeAsync(16);
  }
}

function render(overrides: Partial<ImbueCharacterAttrs> = {}): HTMLElement {
  return mountView(() => m(ImbueCharacter, { size: 120, mood: "idle", ...overrides }));
}

function bodyPath(root: HTMLElement): string {
  return root.querySelector("[data-character-path]")?.getAttribute("d") ?? "";
}

beforeEach(() => {
  setReducedMotion(false);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  unmountViews();
});

describe("the character", () => {
  it("draws a body", async () => {
    const root = render();
    await runFrames(2);
    expect(bodyPath(root).length).toBeGreaterThan(0);
  });

  it("keeps moving while it sits there", async () => {
    const root = render();
    await runFrames(2);
    const first = bodyPath(root);
    await runFrames(40);
    expect(bodyPath(root)).not.toEqual(first);
  });

  it("answers a press", async () => {
    const root = render();
    await runFrames(2);
    const svg = root.querySelector("svg") as SVGSVGElement;
    const untouched = bodyPath(root);
    svg.dispatchEvent(new window.PointerEvent("pointerdown", { pointerId: 1, bubbles: true }));
    await runFrames(3);
    expect(bodyPath(root)).not.toEqual(untouched);
  });

  it("casts a shadow unless told not to", async () => {
    const root = render();
    await runFrames(2);
    expect(root.querySelector("[data-character-shadow]")).not.toBeNull();
    unmountViews();

    const flat = render({ shadow: false });
    await runFrames(2);
    expect(flat.querySelector("[data-character-shadow]")).toBeNull();
  });

  it("stops animating when it goes away", async () => {
    const root = render();
    await runFrames(2);
    const path = root.querySelector("[data-character-path]") as SVGPathElement;
    unmountViews();
    const parked = path.getAttribute("d");
    await runFrames(20);
    expect(path.getAttribute("d")).toEqual(parked);
  });
});

describe("wearing the theme", () => {
  it("is the avatar part, drawn in the theme's avatar colours unless told a colour", () => {
    const themed = render();
    const svg = themed.querySelector("svg") as SVGSVGElement;
    expect(svg.getAttribute("data-part")).toBe("avatar");
    expect(svg.getAttribute("data-avatar-kind")).toBe("character");
    expect((themed.querySelector("[data-character-path]") as SVGPathElement).style.fill).toContain(
      "var(--desk-avatar-color",
    );
    expect((themed.querySelector("stop") as SVGStopElement).style.stopColor).toContain(
      "var(--desk-avatar-shadow-color",
    );
    unmountViews();

    const told = render({ color: "#123456" });
    expect((told.querySelector("[data-character-path]") as SVGPathElement).style.fill).toBe("rgb(18, 52, 86)");
  });
});

describe("the user arriving", () => {
  /** The lean the frame's transform is holding, in degrees. */
  function leanOf(root: HTMLElement): number {
    const transform = root.querySelector("[data-character-body]")?.getAttribute("transform") ?? "";
    const match = /rotate\((-?[\d.]+)/.exec(transform);
    return match === null ? 0 : Number(match[1]);
  }

  it("draws the character up, and slouches it back when the user leaves", async () => {
    let isAttending = false;
    const root = mountView(() => m(ImbueCharacter, { size: 120, mood: "idle", isAttending }));
    await runFrames(30);
    const slouched = leanOf(root);
    expect(slouched).toBeGreaterThan(0);

    isAttending = true;
    m.redraw.sync();
    await runFrames(60);
    const drawnUp = leanOf(root);
    expect(drawnUp).toBeLessThan(slouched);

    isAttending = false;
    m.redraw.sync();
    await runFrames(60);
    expect(leanOf(root)).toBeGreaterThan(drawnUp);
  });
});

describe("being chosen", () => {
  /** The highest the body got off its resting spot over `count` frames, in viewBox units. */
  async function highestOver(root: HTMLElement, count: number): Promise<number> {
    let highest = 0;
    for (let i = 0; i < count; i++) {
      await runFrames(1);
      const transform = root.querySelector("[data-character-body]")?.getAttribute("transform") ?? "";
      const y = Number(/translate\(-?[\d.]+ (-?[\d.]+)\)/.exec(transform)?.[1] ?? 0);
      highest = Math.max(highest, -y);
    }
    return highest;
  }

  it("jumps as it appears when it has just been chosen", async () => {
    const root = render({ isArriving: true });
    expect(await highestOver(root, 45)).toBeGreaterThan(40);
  });

  it("stays on the floor when it appears any other way", async () => {
    // A page load, or its entry drawn somewhere else: only the float moves it.
    const root = render();
    expect(await highestOver(root, 45)).toBeLessThan(5);
  });

  it("does not jump when motion is reduced", async () => {
    setReducedMotion(true);
    const root = render({ isArriving: true });
    expect(await highestOver(root, 45)).toBeLessThan(5);
  });
});

describe("a mouse resting on it", () => {
  /** Where the body is drawn, in viewBox units. */
  function placeOf(root: HTMLElement): { x: number; y: number } {
    const transform = root.querySelector("[data-character-body]")?.getAttribute("transform") ?? "";
    const match = /translate\((-?[\d.]+) (-?[\d.]+)\)/.exec(transform);
    return { x: Number(match?.[1] ?? 0), y: Number(match?.[2] ?? 0) };
  }

  /** Mount it at 100px square, so a pixel is two viewBox units. */
  function sized(): { root: HTMLElement; svg: SVGSVGElement } {
    const root = render({ size: 100 });
    const svg = root.querySelector("svg") as SVGSVGElement;
    svg.getBoundingClientRect = () => ({ left: 0, top: 0, width: 100, height: 100 }) as DOMRect;
    return { root, svg };
  }

  function pointer(svg: SVGSVGElement, type: string, pointerType: string, x: number, y: number): void {
    svg.dispatchEvent(new window.PointerEvent(type, { pointerId: 1, pointerType, clientX: x, clientY: y }));
  }

  it("nudges the body away from it, and lets go when it leaves", async () => {
    const { root, svg } = sized();
    await runFrames(2);
    const rest = placeOf(root).x;
    // In from the right edge: the body moves left by about 2px, 4 units.
    pointer(svg, "pointerenter", "mouse", 100, 50);
    await runFrames(60);
    expect(placeOf(root).x - rest).toBeLessThan(-3);
    expect(placeOf(root).x - rest).toBeGreaterThan(-5);

    pointer(svg, "pointerleave", "mouse", 100, 50);
    await runFrames(60);
    expect(Math.abs(placeOf(root).x - rest)).toBeLessThan(0.5);
  });

  it("holds where the mouse came in, however it moves across", async () => {
    const { root, svg } = sized();
    await runFrames(2);
    pointer(svg, "pointerenter", "mouse", 100, 50);
    await runFrames(60);
    const held = placeOf(root);
    // Across to the left edge: still shied leftward from the right, where it came in.
    pointer(svg, "pointermove", "mouse", 50, 50);
    pointer(svg, "pointermove", "mouse", 0, 50);
    await runFrames(60);
    expect(placeOf(root).x).toBeCloseTo(held.x, 0);
  });

  it("is a mouse's alone: a finger does not hover", async () => {
    const { root, svg } = sized();
    await runFrames(2);
    const rest = placeOf(root).x;
    pointer(svg, "pointerenter", "touch", 100, 50);
    await runFrames(60);
    expect(Math.abs(placeOf(root).x - rest)).toBeLessThan(0.5);
  });
});

describe("reduced motion", () => {
  it("draws the character but holds it still", async () => {
    setReducedMotion(true);
    const root = render();
    await runFrames(2);
    const held = bodyPath(root);
    expect(held.length).toBeGreaterThan(0);
    await runFrames(40);
    expect(bodyPath(root)).toEqual(held);
  });

  it("does not answer a press", async () => {
    setReducedMotion(true);
    const root = render();
    await runFrames(2);
    const svg = root.querySelector("svg") as SVGSVGElement;
    const held = bodyPath(root);
    svg.dispatchEvent(new window.PointerEvent("pointerdown", { pointerId: 1, bubbles: true }));
    await runFrames(5);
    expect(bodyPath(root)).toEqual(held);
  });
});

describe("the design id", () => {
  it("is the one the avatar catalog lists", () => {
    // The shell's `LIVE_DESIGN_ID`; the two are matched by value, not by import.
    expect(IMBUE_CHARACTER_DESIGN_ID).toEqual("imbue-character");
  });
});
