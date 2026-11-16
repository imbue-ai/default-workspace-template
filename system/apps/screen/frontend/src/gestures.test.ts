import { describe, expect, it } from "vitest";
import { GestureClassifier, LONG_PRESS_MS, SCROLL_STEP_PX } from "./gestures";
import type { Gesture } from "./gestures";

function sample(pointerId: number, x: number, y: number, timeMs: number) {
  return { pointerId, x, y, timeMs };
}

function kinds(gestures: Gesture[]): string[] {
  return gestures.filter((gesture) => gesture.kind !== "none").map((gesture) => gesture.kind);
}

describe("GestureClassifier", () => {
  it("a short still press and release is a tap at the press point", () => {
    const classifier = new GestureClassifier();
    classifier.down(sample(1, 100, 100, 0));
    classifier.move(sample(1, 103, 102, 50));
    expect(classifier.up(sample(1, 103, 102, 120))).toEqual({ kind: "tap", at: { x: 100, y: 100 } });
  });

  it("a press held past the long-press time is a right click, and its release is nothing", () => {
    const classifier = new GestureClassifier();
    classifier.down(sample(1, 40, 60, 0));
    expect(classifier.longPress()).toEqual({ kind: "long-press", at: { x: 40, y: 60 } });
    expect(classifier.longPress()).toEqual({ kind: "none" });
    expect(classifier.up(sample(1, 40, 60, LONG_PRESS_MS + 100))).toEqual({ kind: "none" });
  });

  it("a slow release with no movement past the tap time is nothing when no long press fired", () => {
    const classifier = new GestureClassifier();
    classifier.down(sample(1, 40, 60, 0));
    expect(classifier.up(sample(1, 40, 60, 400))).toEqual({ kind: "none" });
  });

  it("one finger travelling past the threshold pans by its movement, ends where it lifts, and never taps", () => {
    const classifier = new GestureClassifier();
    classifier.down(sample(1, 100, 100, 0));
    expect(classifier.move(sample(1, 104, 100, 20))).toEqual({ kind: "none" });
    expect(classifier.move(sample(1, 120, 100, 40))).toEqual({
      kind: "pan",
      start: { x: 100, y: 100 },
      at: { x: 120, y: 100 },
      delta: { x: 16, y: 0 },
    });
    expect(classifier.move(sample(1, 125, 110, 60))).toEqual({
      kind: "pan",
      start: { x: 100, y: 100 },
      at: { x: 125, y: 110 },
      delta: { x: 5, y: 10 },
    });
    expect(classifier.longPress()).toEqual({ kind: "none" });
    expect(classifier.up(sample(1, 125, 110, 80))).toEqual({ kind: "pan-end", at: { x: 125, y: 110 } });
  });

  it("a pan ends where the finger was when a second finger joins or the pointer is cancelled", () => {
    const joined = new GestureClassifier();
    joined.down(sample(1, 100, 100, 0));
    joined.move(sample(1, 130, 100, 20));
    expect(joined.down(sample(2, 200, 100, 30))).toEqual({ kind: "pan-end", at: { x: 130, y: 100 } });
    expect(joined.up(sample(2, 200, 100, 40))).toEqual({ kind: "none" });
    expect(joined.up(sample(1, 130, 100, 50))).toEqual({ kind: "none" });
    const cancelled = new GestureClassifier();
    cancelled.down(sample(1, 100, 100, 0));
    cancelled.move(sample(1, 130, 100, 20));
    expect(cancelled.cancel(1)).toEqual({ kind: "pan-end", at: { x: 130, y: 100 } });
    const stillTap = new GestureClassifier();
    stillTap.down(sample(1, 100, 100, 0));
    expect(stillTap.cancel(1)).toEqual({ kind: "none" });
  });

  it("two fingers moving together scroll in wheel steps against their travel", () => {
    const classifier = new GestureClassifier();
    classifier.down(sample(1, 100, 100, 0));
    classifier.down(sample(2, 200, 100, 10));
    const gestures: Gesture[] = [];
    for (let step = 1; step <= 4; step++) {
      gestures.push(classifier.move(sample(1, 100, 100 + step * 15, 20 * step)));
      gestures.push(classifier.move(sample(2, 200, 100 + step * 15, 20 * step + 5)));
    }
    const scrolls = gestures.filter((gesture) => gesture.kind === "scroll");
    expect(scrolls.length).toBeGreaterThan(0);
    const total = scrolls.reduce((sum, gesture) => sum + (gesture.kind === "scroll" ? gesture.steps.y : 0), 0);
    expect(total).toBe(-Math.trunc(60 / SCROLL_STEP_PX));
    expect(kinds(gestures)).not.toContain("pan");
    expect(classifier.up(sample(1, 100, 160, 200))).toEqual({ kind: "none" });
    expect(classifier.up(sample(2, 200, 160, 210))).toEqual({ kind: "none" });
  });

  it("two fingers moving apart pinch about their centre", () => {
    const classifier = new GestureClassifier();
    classifier.down(sample(1, 100, 100, 0));
    classifier.down(sample(2, 200, 100, 10));
    const gesture = classifier.move(sample(2, 300, 100, 30));
    expect(gesture.kind).toBe("pinch");
    if (gesture.kind === "pinch") {
      expect(gesture.factor).toBeCloseTo(2);
      expect(gesture.about).toEqual({ x: 200, y: 100 });
    }
  });

  it("a second finger makes the sequence two-finger: no tap even after it lifts", () => {
    const classifier = new GestureClassifier();
    classifier.down(sample(1, 100, 100, 0));
    classifier.down(sample(2, 110, 100, 10));
    classifier.up(sample(2, 110, 100, 50));
    expect(classifier.longPress()).toEqual({ kind: "none" });
    expect(classifier.up(sample(1, 100, 100, 100))).toEqual({ kind: "none" });
  });
});
