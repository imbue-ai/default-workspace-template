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
