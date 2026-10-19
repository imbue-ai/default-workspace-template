// @vitest-environment jsdom
/** Which viewers get the phone layout (the Screen is never resized from one, and a small desktop window is not
 *  one), and what the phone's controls send: a one-finger drag as a touch on the Screen, or a pan once zoomed, and
 *  the Scroll buttons as wheel steps. */
import { describe, expect, it } from "vitest";

import { PHONE_MEDIA_QUERY, TOUCH_MEDIA_QUERY } from "@imbue/workspace-ui/src/device_queries";
import { MOVE_THRESHOLD_PX, PINCH_THRESHOLD_PX } from "./gestures";
import { MobileControls, SCROLL_BUTTON_STEPS, isPhoneViewer } from "./mobile";
import type { MobileActions } from "./mobile";
import { FakeScheduler } from "./testing/fakeScheduler";
import type { Point } from "./view_transform";

class RecordingActions implements MobileActions {
  readonly log: string[] = [];
  isTouchSupported = true;

  tapAt(clientX: number, clientY: number): void {
    this.log.push(`tap ${clientX},${clientY}`);
  }

  clickAt(clientX: number, clientY: number, button: number): void {
    this.log.push(`click${button} ${clientX},${clientY}`);
  }

  scroll(steps: Point): void {
    this.log.push(`scroll ${steps.x},${steps.y}`);
  }

  touchBegin(clientX: number, clientY: number): boolean {
    if (!this.isTouchSupported) return false;
    this.log.push(`touchbegin ${clientX},${clientY}`);
    return true;
  }

  touchMove(clientX: number, clientY: number): void {
    this.log.push(`touchmove ${clientX},${clientY}`);
  }

  touchEnd(clientX: number, clientY: number): void {
    this.log.push(`touchend ${clientX},${clientY}`);
  }
}

function pointer(type: string, pointerId: number, clientX: number, clientY: number): PointerEvent {
  return new PointerEvent(type, {
    pointerId,
    pointerType: "touch",
    clientX,
    clientY,
    bubbles: true,
    cancelable: true,
  });
}

function build(): {
  controls: MobileControls;
  actions: RecordingActions;
  stage: HTMLDivElement;
  video: HTMLDivElement;
} {
  const stage = document.createElement("div");
  // jsdom lays nothing out; the pan clamps to the stage's size, so the stage needs one.
  stage.getBoundingClientRect = () =>
    ({ left: 0, top: 0, width: 400, height: 600, right: 400, bottom: 600, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
  const video = document.createElement("div");
  stage.append(video);
  document.body.append(stage);
  const actions = new RecordingActions();
  const controls = new MobileControls(
    stage,
    video,
    actions,
    { keyDown: () => undefined, keyUp: () => undefined },
    new FakeScheduler(),
  );
  controls.setEnabled(true);
  return { controls, actions, stage, video };
}

function dragOneFinger(stage: HTMLElement): void {
  stage.dispatchEvent(pointer("pointerdown", 1, 100, 300));
  stage.dispatchEvent(pointer("pointermove", 1, 100, 300 - MOVE_THRESHOLD_PX - 2));
  stage.dispatchEvent(pointer("pointermove", 1, 100, 200));
  stage.dispatchEvent(pointer("pointerup", 1, 100, 200));
}

describe("MobileControls", () => {
  it("sends a one-finger drag as a touch held and moved on the Screen", () => {
    const { actions, stage } = build();
    dragOneFinger(stage);
    expect(actions.log).toEqual([
      "touchbegin 100,300",
      `touchmove 100,${300 - MOVE_THRESHOLD_PX - 2}`,
      "touchmove 100,200",
      "touchend 100,200",
    ]);
  });

  it("pans the view instead once the video is pinch-zoomed, and when no touch can begin", () => {
    const { controls, actions, stage, video } = build();
    stage.dispatchEvent(pointer("pointerdown", 1, 100, 300));
    stage.dispatchEvent(pointer("pointerdown", 2, 200, 300));
    stage.dispatchEvent(pointer("pointermove", 2, 200 + PINCH_THRESHOLD_PX * 4, 300));
    stage.dispatchEvent(pointer("pointerup", 2, 200 + PINCH_THRESHOLD_PX * 4, 300));
    stage.dispatchEvent(pointer("pointerup", 1, 100, 300));
    expect(controls.viewTransform.zoom).toBeGreaterThan(1);
    const transformBefore = video.style.transform;
    dragOneFinger(stage);
    expect(actions.log).toEqual([]);
    expect(video.style.transform).not.toBe(transformBefore);
    const noDriver = build();
    noDriver.actions.isTouchSupported = false;
    dragOneFinger(noDriver.stage);
    expect(noDriver.actions.log).toEqual([]);
  });

  it("sends nothing for a drag while not controlling", () => {
    const { controls, actions, stage } = build();
    controls.setEnabled(false);
    dragOneFinger(stage);
    expect(actions.log).toEqual([]);
  });

  it("scrolls by wheel steps from the Scroll Up and Scroll Down buttons", () => {
    const { controls, actions } = build();
    const up = controls.bar.querySelector<HTMLButtonElement>('[data-screen-scroll="up"]');
    const down = controls.bar.querySelector<HTMLButtonElement>('[data-screen-scroll="down"]');
    up?.click();
    down?.click();
    expect(actions.log).toEqual([`scroll 0,${-SCROLL_BUTTON_STEPS}`, `scroll 0,${SCROLL_BUTTON_STEPS}`]);
    controls.setEnabled(false);
    down?.click();
    expect(actions.log.length).toBe(2);
  });
});

function matching(...queries: string[]): (query: string) => boolean {
  return (query) => queries.includes(query);
}

describe("isPhoneViewer", () => {
  it("is a phone-sized viewport with a finger as the primary pointer", () => {
    expect(isPhoneViewer(matching(PHONE_MEDIA_QUERY, TOUCH_MEDIA_QUERY))).toBe(true);
  });

  it("is not a small desktop window", () => {
    expect(isPhoneViewer(matching(PHONE_MEDIA_QUERY))).toBe(false);
  });

  it("is not a touchscreen with a larger viewport", () => {
    expect(isPhoneViewer(matching(TOUCH_MEDIA_QUERY))).toBe(false);
    expect(isPhoneViewer(matching())).toBe(false);
  });
});
