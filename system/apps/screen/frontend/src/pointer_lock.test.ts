import { afterEach, describe, expect, it, vi } from "vitest";
import { IS_RELATIVE_MOTION_SUPPORTED, PointerLockControl } from "./pointer_lock";

const controls: PointerLockControl[] = [];

function makeControl(locked: { element: Element | null }, onRelativeMove = vi.fn()) {
  const stage = document.createElement("div");
  document.body.append(stage);
  const control = new PointerLockControl({ stage, onRelativeMove, readLockedElement: () => locked.element });
  controls.push(control);
  return { stage, control, onRelativeMove };
}

afterEach(() => {
  document.body.replaceChildren();
  controls.length = 0;
});

describe("PointerLockControl", () => {
  it("stays hidden while neko has no relative-motion event, controlling or not", () => {
    const { control } = makeControl({ element: null });
    expect(IS_RELATIVE_MOTION_SUPPORTED).toBe(false);
    control.setAvailable(true);
    expect(control.button.hidden).toBe(true);
    control.setAvailable(false);
    expect(control.button.hidden).toBe(true);
  });

  it("forwards relative motion only while the stage holds the lock, and learns of its release", () => {
    const locked: { element: Element | null } = { element: null };
    const { stage, control, onRelativeMove } = makeControl(locked);
    stage.dispatchEvent(new MouseEvent("mousemove", { movementX: 4, movementY: -2 }));
    expect(onRelativeMove).not.toHaveBeenCalled();
    locked.element = stage;
    document.dispatchEvent(new Event("pointerlockchange"));
    expect(control.locked).toBe(true);
    stage.dispatchEvent(new MouseEvent("mousemove", { movementX: 4, movementY: -2 }));
    expect(onRelativeMove).toHaveBeenCalledWith(4, -2);
    locked.element = null;
    document.dispatchEvent(new Event("pointerlockchange"));
    expect(control.locked).toBe(false);
    stage.dispatchEvent(new MouseEvent("mousemove", { movementX: 1, movementY: 1 }));
    expect(onRelativeMove).toHaveBeenCalledTimes(1);
  });
});
