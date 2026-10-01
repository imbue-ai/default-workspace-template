// @vitest-environment jsdom
/**
 * A tap that can be held: a still press is a tap, a press held for the long-press time is a long press whose
 * release is not also a tap, and a press that wanders past the threshold is neither.
 */
import "../../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LONG_PRESS_MS } from "../../gestures/pointerGestures";
import { longPressAttrs } from "./longPress";

const THRESHOLD_PX = 8;
let onTap = vi.fn<() => void>();
let onLongPress = vi.fn<(target: HTMLElement) => void>();

function target(): HTMLElement {
  const root = mountView(() =>
    m("button", { "data-target": "", ...longPressAttrs({ onTap, onLongPress, thresholdPx: THRESHOLD_PX }) }),
  );
  return root.querySelector("[data-target]") as HTMLElement;
}

function pointer(element: HTMLElement, type: string, clientX: number): void {
  element.dispatchEvent(new PointerEvent(type, { bubbles: true, button: 0, pointerId: 1, clientX, clientY: 0 }));
}

beforeEach(() => {
  vi.useFakeTimers();
  onTap = vi.fn();
  onLongPress = vi.fn();
});

afterEach(() => {
  unmountViews();
  vi.useRealTimers();
});

describe("a press that can be held", () => {
  it("runs nothing for a press that moved past the threshold, even when its release clicks", () => {
    const element = target();
    pointer(element, "pointerdown", 0);
    pointer(element, "pointermove", THRESHOLD_PX * 3);
    pointer(element, "pointerup", THRESHOLD_PX * 3);
    element.click();
    vi.advanceTimersByTime(LONG_PRESS_MS);
    expect(onTap).not.toHaveBeenCalled();
    expect(onLongPress).not.toHaveBeenCalled();

    // The next click with no press before it (a keyboard's) is a tap again.
    element.click();
    expect(onTap).toHaveBeenCalledTimes(1);
  });

  it("runs the long press for a held press, and its release is not also a tap", () => {
    const element = target();
    pointer(element, "pointerdown", 0);
    vi.advanceTimersByTime(LONG_PRESS_MS);
    pointer(element, "pointerup", 0);
    element.click();
    expect(onLongPress).toHaveBeenCalledWith(element);
    expect(onTap).not.toHaveBeenCalled();

    pointer(element, "pointerdown", 0);
    pointer(element, "pointerup", 0);
    element.click();
    expect(onTap).toHaveBeenCalledTimes(1);
  });
});
