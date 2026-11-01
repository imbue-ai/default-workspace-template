// @vitest-environment jsdom
/** Pointer mapping into the letterboxed Screen, and what the relay sends to neko and when. */
import { beforeEach, describe, expect, it } from "vitest";

import { InputRelay, XK_CONTROL_L, XK_W, screenPointFromStage, touchPressureOf } from "./input";
import type { NekoSocket } from "./neko_socket";

const SCREEN = { width: 1920, height: 1080 };

function rect(left: number, top: number, width: number, height: number): DOMRect {
  return {
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    x: left,
    y: top,
    toJSON: () => ({}),
  };
}

describe("screenPointFromStage", () => {
  it("maps through the letterbox of a stage wider than the Screen", () => {
    // 960x540 of video centred in 1160x540: a 100 px band on each side.
    const stage = rect(0, 0, 1160, 540);
    expect(screenPointFromStage(100, 0, stage, SCREEN)).toEqual({ x: 0, y: 0 });
    expect(screenPointFromStage(580, 270, stage, SCREEN)).toEqual({ x: 960, y: 540 });
    expect(screenPointFromStage(50, 270, stage, SCREEN)).toBeNull();
    expect(screenPointFromStage(1100, 270, stage, SCREEN)).toBeNull();
  });

  it("maps through the letterbox of a stage taller than the Screen, offset by the stage's position", () => {
    // 960x540 of video centred in 960x740 at (10, 20): a 100 px band above and below.
    const stage = rect(10, 20, 960, 740);
    expect(screenPointFromStage(10, 120, stage, SCREEN)).toEqual({ x: 0, y: 0 });
    expect(screenPointFromStage(490, 390, stage, SCREEN)).toEqual({ x: 960, y: 540 });
    expect(screenPointFromStage(490, 50, stage, SCREEN)).toBeNull();
  });

  it("answers nothing for an unsized stage or Screen", () => {
    expect(screenPointFromStage(5, 5, rect(0, 0, 0, 0), SCREEN)).toBeNull();
    expect(screenPointFromStage(5, 5, rect(0, 0, 100, 100), { width: 0, height: 0 })).toBeNull();
  });
});

interface Sent {
  event: string;
  payload: unknown;
}

class RecordingSocket {
  readonly sent: Sent[] = [];

  send(event: string, payload?: unknown): void {
    this.sent.push({ event, payload });
  }
}

describe("touchPressureOf", () => {
  it("scales a pointer event's pressure to neko's byte, never reading as no contact", () => {
    expect(touchPressureOf(0.5)).toBe(128);
    expect(touchPressureOf(1)).toBe(255);
    expect(touchPressureOf(0)).toBe(1);
    expect(touchPressureOf(2)).toBe(255);
  });
});

describe("InputRelay", () => {
  let socket: RecordingSocket;
  let stage: HTMLDivElement;
  let relay: InputRelay;

  beforeEach(() => {
    socket = new RecordingSocket();
    stage = document.createElement("div");
    stage.tabIndex = 0;
    stage.getBoundingClientRect = () => rect(0, 0, 960, 540);
    document.body.append(stage);
    relay = new InputRelay(socket as unknown as NekoSocket, stage);
    relay.screen = SCREEN;
  });

  it("hands the stage's focus to its hidden key sink, an editable element, and keeps nothing typed into it", () => {
    stage.focus();
    expect(document.activeElement).toBe(relay.keySink);
    expect(relay.keySink.tagName).toBe("INPUT");
    expect(relay.keySink.type).toBe("text");
    relay.keySink.value = "n";
    relay.keySink.dispatchEvent(new Event("input", { bubbles: true }));
    expect(relay.keySink.value).toBe("");
  });

  it("leaves the sink to an IME while it composes and empties it once the composition ends", () => {
    relay.keySink.value = "に";
    relay.keySink.dispatchEvent(new InputEvent("input", { bubbles: true, isComposing: true }));
    expect(relay.keySink.value).toBe("に");
    relay.keySink.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: "に" }));
    expect(relay.keySink.value).toBe("");
  });

  it("disposed, it takes its sink out of the stage, releases what it held and sends nothing more", () => {
    relay.setEnabled(true);
    relay.keyDown(XK_CONTROL_L);
    socket.sent.length = 0;
    relay.dispose();
    expect(stage.contains(relay.keySink)).toBe(false);
    expect(socket.sent).toEqual([{ event: "control/keyup", payload: { keysym: XK_CONTROL_L } }]);
    socket.sent.length = 0;
    relay.keyDown(XK_CONTROL_L);
    expect(socket.sent).toEqual([]);
  });

  it("releases held keys when focus leaves the stage, not when it moves into the sink", () => {
    relay.setEnabled(true);
    relay.keyDown(XK_CONTROL_L);
    socket.sent.length = 0;
    stage.dispatchEvent(new FocusEvent("focusout", { relatedTarget: relay.keySink, bubbles: true }));
    expect(socket.sent).toEqual([]);
    stage.dispatchEvent(new FocusEvent("focusout", { relatedTarget: null, bubbles: true }));
    expect(socket.sent).toEqual([{ event: "control/keyup", payload: { keysym: XK_CONTROL_L } }]);
  });

  it("sends nothing while disabled", () => {
    stage.dispatchEvent(new MouseEvent("mousedown", { clientX: 10, clientY: 10, button: 0 }));
    stage.dispatchEvent(new MouseEvent("mouseup", { clientX: 10, clientY: 10, button: 0 }));
    relay.sendCloseChord();
    expect(socket.sent).toEqual([]);
  });

  it("flushes the pointer's position before a button and maps DOM buttons to X buttons", () => {
    relay.setEnabled(true);
    stage.dispatchEvent(new MouseEvent("mousemove", { clientX: 480, clientY: 270 }));
    stage.dispatchEvent(new MouseEvent("mousedown", { clientX: 480, clientY: 270, button: 2 }));
    stage.dispatchEvent(new MouseEvent("mouseup", { clientX: 480, clientY: 270, button: 2 }));
    expect(socket.sent).toEqual([
      { event: "control/move", payload: { x: 960, y: 540 } },
      { event: "control/buttondown", payload: { code: 3 } },
      { event: "control/move", payload: { x: 960, y: 540 } },
      { event: "control/buttonup", payload: { code: 3 } },
    ]);
  });

  it("ignores a press outside the drawn video", () => {
    relay.setEnabled(true);
    stage.getBoundingClientRect = () => rect(0, 0, 1160, 540);
    stage.dispatchEvent(new MouseEvent("mousedown", { clientX: 20, clientY: 270, button: 0 }));
    expect(socket.sent).toEqual([]);
  });

  it("sends a tap as a touch begun and ended at the mapped point with the pressure as a byte", () => {
    relay.setEnabled(true);
    relay.isTouchSupported = true;
    relay.tapAt(480, 270, 0.5);
    const touch = { touch_id: 0, x: 960, y: 540, pressure: 128 };
    expect(socket.sent).toEqual([
      { event: "control/touchbegin", payload: touch },
      { event: "control/touchend", payload: touch },
    ]);
  });

  it("gives each tap its own touch id, within the driver's sixteen bits", () => {
    relay.setEnabled(true);
    relay.isTouchSupported = true;
    relay.tapAt(480, 270, 0.5);
    relay.tapAt(480, 270, 0.5);
    const ids = socket.sent.map((sent) => (sent.payload as { touch_id: number }).touch_id);
    expect(ids).toEqual([0, 0, 1, 1]);
    expect(Math.max(...ids)).toBeLessThanOrEqual(0xffff);
  });

  it("holds a touch drag on the Screen: begun where the finger started, moved with it, ended where it lifted", () => {
    relay.setEnabled(true);
    relay.isTouchSupported = true;
    expect(relay.beginTouchDrag(480, 270, 0.5)).toBe(true);
    relay.moveTouchDrag(480, 200, 0.25);
    // A sample off the drawn video is skipped; the touch stays where it was.
    relay.moveTouchDrag(-50, 200, 0.25);
    relay.endTouchDrag(480, 100, 0);
    expect(socket.sent).toEqual([
      { event: "control/touchbegin", payload: { touch_id: 0, x: 960, y: 540, pressure: 128 } },
      { event: "control/touchupdate", payload: { touch_id: 0, x: 960, y: 400, pressure: 64 } },
      { event: "control/touchend", payload: { touch_id: 0, x: 960, y: 200, pressure: 1 } },
    ]);
    // Nothing more goes out once the drag has ended.
    relay.moveTouchDrag(480, 150, 0.5);
    relay.endTouchDrag(480, 150, 0.5);
    expect(socket.sent.length).toBe(3);
  });

  it("ends a held touch where it last was when control is lost, and begins none without the driver", () => {
    relay.setEnabled(true);
    expect(relay.beginTouchDrag(480, 270, 0.5)).toBe(false);
    relay.isTouchSupported = true;
    expect(relay.beginTouchDrag(480, 270, 0.5)).toBe(true);
    relay.moveTouchDrag(480, 200, 0.5);
    relay.setEnabled(false);
    expect(socket.sent.slice(-1)).toEqual([
      { event: "control/touchend", payload: { touch_id: 0, x: 960, y: 400, pressure: 1 } },
    ]);
  });

  it("sends a tap as a left click where neko has no input driver", () => {
    relay.setEnabled(true);
    relay.tapAt(480, 270, 0.5);
    expect(socket.sent).toEqual([
      { event: "control/move", payload: { x: 960, y: 540 } },
      { event: "control/buttondown", payload: { code: 1 } },
      { event: "control/buttonup", payload: { code: 1 } },
    ]);
  });

  it("sends no touch for a tap outside the drawn video or while disabled", () => {
    relay.isTouchSupported = true;
    relay.tapAt(480, 270, 0.5);
    relay.setEnabled(true);
    stage.getBoundingClientRect = () => rect(0, 0, 1160, 540);
    relay.tapAt(20, 270, 0.5);
    expect(socket.sent).toEqual([]);
  });

  it("sends a wheel's steps in neko's sign, the opposite of the DOM's", () => {
    relay.setEnabled(true);
    // A positive deltaY scrolls the content down in the DOM; neko's positive delta_y is wheel up (X button 4).
    stage.dispatchEvent(new WheelEvent("wheel", { deltaY: 100, deltaX: 0, deltaMode: 0, cancelable: true }));
    stage.dispatchEvent(new WheelEvent("wheel", { deltaY: 0, deltaX: 100, deltaMode: 0, cancelable: true }));
    expect(socket.sent).toEqual([
      { event: "control/scroll", payload: { delta_x: 0, delta_y: -1, control_key: false } },
      { event: "control/scroll", payload: { delta_x: -1, delta_y: 0, control_key: false } },
    ]);
  });

  it("sends the phone's scroll steps in neko's sign too", () => {
    relay.setEnabled(true);
    relay.scroll({ x: 0, y: 2 });
    relay.scroll({ x: -1, y: 0 });
    expect(socket.sent).toEqual([
      { event: "control/scroll", payload: { delta_x: 0, delta_y: -2, control_key: false } },
      { event: "control/scroll", payload: { delta_x: 1, delta_y: 0, control_key: false } },
    ]);
  });

  it("sends the close chord as Ctrl+W, pressed and released in order", () => {
    relay.setEnabled(true);
    relay.sendCloseChord();
    expect(socket.sent).toEqual([
      { event: "control/keydown", payload: { keysym: XK_CONTROL_L } },
      { event: "control/keydown", payload: { keysym: XK_W } },
      { event: "control/keyup", payload: { keysym: XK_W } },
      { event: "control/keyup", payload: { keysym: XK_CONTROL_L } },
    ]);
  });
});
