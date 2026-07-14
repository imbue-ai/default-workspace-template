import { describe, expect, it } from "vitest";
import { computeViewportClamp } from "./mobile-viewport";

describe("computeViewportClamp", () => {
  it("clamps to the visual viewport when the keyboard eats a large chunk", () => {
    // iPhone-ish: 844px layout viewport, keyboard shrinks the visual viewport.
    expect(computeViewportClamp(844, 500)).toBe(500);
    expect(computeViewportClamp(844, 493.7)).toBe(494);
  });

  it("leaves dvh in charge when the viewports agree", () => {
    expect(computeViewportClamp(844, 844)).toBeNull();
  });

  it("ignores sub-threshold gaps from rounding or minor browser UI", () => {
    expect(computeViewportClamp(844, 843.5)).toBeNull();
    expect(computeViewportClamp(844, 800)).toBeNull();
  });

  it("never clamps when the visual viewport is the larger one", () => {
    expect(computeViewportClamp(700, 844)).toBeNull();
  });
});
