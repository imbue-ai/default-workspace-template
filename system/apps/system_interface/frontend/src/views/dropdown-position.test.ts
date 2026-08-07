import { describe, expect, it } from "vitest";

import { clampDropdownLeft } from "./dropdown-position";

// A comfortably-fitting dropdown; individual tests override what they exercise.
const BASE = {
  labelLeft: 300,
  textInset: 16,
  dropdownWidth: 200,
  viewportWidth: 1000,
  margin: 8,
} as const;

describe("clampDropdownLeft", () => {
  it("aligns its text under the trigger label when there is room (rule 2)", () => {
    // Plenty of room on both sides: the box sits so its inner text (inset 16) starts
    // exactly at the label text.
    expect(clampDropdownLeft(BASE)).toBe(300 - 16);
  });

  it("never bleeds past the left edge -- clamps to the margin (rule 1 over rule 2)", () => {
    // Trigger hard against the left edge: aligning would push the box to negative x.
    const left = clampDropdownLeft({ ...BASE, labelLeft: 4 });
    expect(left).toBe(8);
  });

  it("keeps a full margin even when the label sits exactly at the margin", () => {
    // labelLeft - textInset = -8, well left of the margin: still pinned to the margin.
    const left = clampDropdownLeft({ ...BASE, labelLeft: 8 });
    expect(left).toBe(8);
  });

  it("never bleeds past the right edge -- clamps so the right side keeps its margin", () => {
    // Trigger near the right edge (the effort picker's case): aligning would run the
    // box off the right, so it shifts left to leave `margin` on the right.
    const left = clampDropdownLeft({ ...BASE, labelLeft: 980, dropdownWidth: 200 });
    // maxLeft = 1000 - 8 - 200 = 792; the right edge then sits at 992 = viewport - margin.
    expect(left).toBe(792);
    expect(left + 200).toBe(1000 - 8);
  });

  it("still fully fits the box at a narrow viewport (the bug's scenario)", () => {
    // A 200px box in a 360px viewport with the trigger near the left edge: it aligns
    // to the label (40 - 16 = 24) and still leaves margins on both sides -- the sheared
    // left edge from the bug can no longer happen.
    const left = clampDropdownLeft({ ...BASE, labelLeft: 40, viewportWidth: 360 });
    expect(left).toBe(24);
    expect(left).toBeGreaterThanOrEqual(8);
    expect(left + 200).toBeLessThanOrEqual(360 - 8);
  });

  it("grows the alignment shift as the label moves, until the right clamp binds", () => {
    // A sweep: while there is room the left tracks the label; past the right limit it stops.
    const widths = [100, 400, 700, 900, 999];
    const lefts = widths.map((labelLeft) => clampDropdownLeft({ ...BASE, labelLeft }));
    // Monotonic non-decreasing, and never past the right clamp (792).
    for (let i = 1; i < lefts.length; i++) {
      expect(lefts[i]).toBeGreaterThanOrEqual(lefts[i - 1]);
      expect(lefts[i]).toBeLessThanOrEqual(792);
    }
  });

  it("pins to the left margin when the box is wider than the viewport can hold", () => {
    // Degenerate: box wider than viewport - 2*margin. Rule 1 can't hold both edges, so
    // it favours the left edge (header + first chars visible) rather than the right.
    const left = clampDropdownLeft({
      labelLeft: 500,
      textInset: 16,
      dropdownWidth: 400,
      viewportWidth: 380,
      margin: 8,
    });
    expect(left).toBe(8);
  });
});
