/** Human input to neko over its socket: pointer moves and buttons in Screen coordinates, wheel steps, keys as X11
 *  keysyms through the Guacamole keyboard, and a phone's taps as touches through neko's xf86 input driver when the
 *  server has it (spec section 14). Sent only while this viewer's session is the controller (spec section 11). */

import GuacamoleKeyboard from "@imbue/workspace-ui/src/guacamole_keyboard.js";
import type { KeySink } from "./modifier_latch";
import type { NekoSocket } from "./neko_socket";
import { IDENTITY_TRANSFORM, screenPointFromClient } from "./view_transform";
import type { Point, Size, ViewTransform } from "./view_transform";

const MOVE_COALESCE_MS = 16;
const WHEEL_STEP_PX = 53;
/** neko's ``pressure`` is a byte; a finger on the glass is never zero, and a browser that reports no pressure says
 *  0 or 0.5, so the floor keeps a tap from reading as no contact. */
const MAX_TOUCH_PRESSURE = 255;
const MIN_TOUCH_PRESSURE = 1;
/** The touch ids neko's driver reads from its messages are 16 bits wide. */
const MAX_TOUCH_ID = 0xffff;

export const XK_CONTROL_L = 0xffe3;
export const XK_CONTROL_R = 0xffe4;
export const XK_META_L = 0xffe7;
export const XK_META_R = 0xffe8;
export const XK_SUPER_L = 0xffeb;
export const XK_SUPER_R = 0xffec;
export const XK_W = 0x0077;
export const XK_V = 0x0076;
export const XK_V_UPPER = 0x0056;

export type ScreenGeometry = Size;

/** neko's ``pressure`` byte for a pointer event's pressure (0 to 1). */
export function touchPressureOf(pressure: number): number {
  const scaled = Math.round(pressure * MAX_TOUCH_PRESSURE);
  return Math.min(MAX_TOUCH_PRESSURE, Math.max(MIN_TOUCH_PRESSURE, scaled));
}

/** Where a pointer event lands on the Screen, given the letterboxed video inside the stage; null outside it. */
export function screenPointFromStage(
  clientX: number,
  clientY: number,
  stageRect: DOMRect,
  screen: ScreenGeometry,
  transform: ViewTransform = IDENTITY_TRANSFORM,
): Point | null {
  return screenPointFromClient(clientX, clientY, stageRect, screen, transform);
}

export class InputRelay implements KeySink {
  private readonly keyboard = new GuacamoleKeyboard();
  private readonly held = new Set<number>();
  private pendingMove: Point | null = null;
  private moveTimer = 0;
  private wheel = { x: 0, y: 0, t: 0 };
  private nextTouchId = 0;
  isEnabled = false;
  /** Whether neko runs its xf86 input driver (``touch_events`` of ``system/init``); without it a tap is a click. */
  isTouchSupported = false;
  screen: ScreenGeometry = { width: 0, height: 0 };
  /** The phone's zoom and pan over the video; identity elsewhere. */
  viewTransform: () => ViewTransform = () => IDENTITY_TRANSFORM;

  constructor(
    private readonly socket: NekoSocket,
    private readonly stage: HTMLElement,
    /** Called once per input event sent; the viewer throttles it into the service's input clock. */
    private readonly onInput: () => void = () => undefined,
  ) {
    this.keyboard.onkeydown = (keysym) => {
      if (!this.isEnabled) return true;
      // A paste chord is left to the browser so its ``paste`` event fires with the clipboard's data; the viewer
      // then sets the remote clipboard and taps V itself (spec section 13).
      if (this.isPasteChord(keysym)) return true;
      this.flushMove();
      this.keyDown(keysym);
      return false;
    };
    this.keyboard.onkeyup = (keysym) => {
      if (!this.isEnabled) return;
      if (this.isPasteChord(keysym)) return;
      this.keyUp(keysym);
    };
    this.keyboard.listenTo(stage);
    stage.addEventListener("mousemove", (event) => this.onMove(event));
    stage.addEventListener("mousedown", (event) => this.onButton(event, true));
    stage.addEventListener("mouseup", (event) => this.onButton(event, false));
    stage.addEventListener("contextmenu", (event) => {
      if (this.isEnabled) event.preventDefault();
    });
    stage.addEventListener("wheel", (event) => this.onWheel(event), { passive: false });
    stage.addEventListener("blur", () => this.releaseHeld());
  }

  setEnabled(isEnabled: boolean): void {
    if (this.isEnabled && !isEnabled) this.releaseHeld();
    this.isEnabled = isEnabled;
  }

  private isPasteChord(keysym: number): boolean {
    const isPasteKey = keysym === XK_V || keysym === XK_V_UPPER;
    return isPasteKey && (this.isControlHeld || this.isCommandHeld);
  }

  get isControlHeld(): boolean {
    return this.held.has(XK_CONTROL_L) || this.held.has(XK_CONTROL_R);
  }

  private get isCommandHeld(): boolean {
    return (
      this.held.has(XK_META_L) || this.held.has(XK_META_R) || this.held.has(XK_SUPER_L) || this.held.has(XK_SUPER_R)
    );
  }

  /** Release every key held on the remote (focus left, control left): nothing stays pinned down. */
  releaseHeld(): void {
    this.keyboard.reset();
    for (const keysym of [...this.held]) this.socket.send("control/keyup", { keysym });
    this.held.clear();
  }

  keyDown(keysym: number): void {
    if (!this.isEnabled) return;
    this.held.add(keysym);
    this.socket.send("control/keydown", { keysym });
    this.onInput();
  }

  keyUp(keysym: number): void {
    if (!this.isEnabled) return;
    this.held.delete(keysym);
    this.socket.send("control/keyup", { keysym });
  }

  /** Tap a key, with Ctrl held for it unless the user already holds Ctrl (a paste after the clipboard was set). */
  tapWithControl(keysym: number): void {
    if (!this.isEnabled) return;
    const isControlAlready = this.isControlHeld;
    if (!isControlAlready) this.socket.send("control/keydown", { keysym: XK_CONTROL_L });
    this.socket.send("control/keydown", { keysym });
    this.socket.send("control/keyup", { keysym });
    if (!isControlAlready) this.socket.send("control/keyup", { keysym: XK_CONTROL_L });
    this.onInput();
  }

  /** A click at a client point (the phone's tap or long press), with the X button number. */
  clickAt(clientX: number, clientY: number, button: number): void {
    if (!this.isEnabled) return;
    const point = this.pointAt(clientX, clientY);
    if (point === null) return;
    this.pendingMove = point;
    this.flushMove();
    this.socket.send("control/buttondown", { code: button });
    this.socket.send("control/buttonup", { code: button });
    this.onInput();
  }

  /** A tap at a client point (the phone's): a touch begun and ended there through neko's input driver, so the
   *  Screen sees a touchscreen tap; a click where the server has no driver. ``pressure`` is the pointer event's. */
  tapAt(clientX: number, clientY: number, pressure: number): void {
    if (!this.isEnabled) return;
    if (!this.isTouchSupported) {
      this.clickAt(clientX, clientY, 1);
      return;
    }
    const point = this.pointAt(clientX, clientY);
    if (point === null) return;
    this.flushMove();
    const touch = { touch_id: this.allocateTouchId(), ...point, pressure: touchPressureOf(pressure) };
    this.socket.send("control/touchbegin", touch);
    this.socket.send("control/touchend", touch);
    this.onInput();
  }

  /** A fresh id for one touch: the driver sees sixteen bits of it, and neko refuses an id still begun. */
  private allocateTouchId(): number {
    const touchId = this.nextTouchId;
    this.nextTouchId = (this.nextTouchId + 1) % (MAX_TOUCH_ID + 1);
    return touchId;
  }

  /** Wheel steps (the phone's two-finger scroll). */
  scroll(steps: Point): void {
    if (!this.isEnabled || (steps.x === 0 && steps.y === 0)) return;
    this.socket.send("control/scroll", { delta_x: steps.x, delta_y: steps.y, control_key: false });
    this.onInput();
  }

  /** The screen point under a client point, or null outside the drawn video. */
  pointAt(clientX: number, clientY: number): Point | null {
    return screenPointFromStage(
      clientX,
      clientY,
      this.stage.getBoundingClientRect(),
      this.screen,
      this.viewTransform(),
    );
  }

  /** The close chord: Ctrl+W to the remote rather than the window closing (spec section 11). */
  sendCloseChord(): void {
    if (!this.isEnabled) return;
    this.socket.send("control/keydown", { keysym: XK_CONTROL_L });
    this.socket.send("control/keydown", { keysym: XK_W });
    this.socket.send("control/keyup", { keysym: XK_W });
    this.socket.send("control/keyup", { keysym: XK_CONTROL_L });
  }

  private pointOf(event: MouseEvent): Point | null {
    return this.pointAt(event.clientX, event.clientY);
  }

  private onMove(event: MouseEvent): void {
    if (!this.isEnabled) return;
    const point = this.pointOf(event);
    if (point === null) return;
    this.pendingMove = point;
    if (!this.moveTimer) this.moveTimer = window.setTimeout(() => this.flushMove(), MOVE_COALESCE_MS);
  }

  private flushMove(): void {
    window.clearTimeout(this.moveTimer);
    this.moveTimer = 0;
    if (this.pendingMove === null) return;
    this.socket.send("control/move", this.pendingMove);
    this.pendingMove = null;
    this.onInput();
  }

  private onButton(event: MouseEvent, isDown: boolean): void {
    if (isDown) this.stage.focus();
    if (!this.isEnabled) return;
    const point = this.pointOf(event);
    if (point === null && isDown) return;
    event.preventDefault();
    if (point !== null) this.pendingMove = point;
    this.flushMove();
    // X button numbers: 1 left, 2 middle, 3 right.
    this.socket.send(isDown ? "control/buttondown" : "control/buttonup", { code: event.button + 1 });
    this.onInput();
  }

  private onWheel(event: WheelEvent): void {
    if (!this.isEnabled) return;
    event.preventDefault();
    const isFirst = event.timeStamp - this.wheel.t > 250;
    if (isFirst) this.wheel = { x: 0, y: 0, t: event.timeStamp };
    const factor =
      event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16 : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? 400 : 1;
    this.wheel.x += event.deltaX * factor;
    this.wheel.y += event.deltaY * factor;
    const steps = (value: number) => (isFirst ? Math.sign(value) : Math.trunc(value / WHEEL_STEP_PX));
    const dx = steps(this.wheel.x);
    const dy = steps(this.wheel.y);
    if (dx) this.wheel.x -= isFirst ? this.wheel.x : dx * WHEEL_STEP_PX;
    if (dy) this.wheel.y -= isFirst ? this.wheel.y : dy * WHEEL_STEP_PX;
    if (dx || dy) {
      this.flushMove();
      this.socket.send("control/scroll", { delta_x: dx, delta_y: dy, control_key: event.ctrlKey });
      this.onInput();
    }
  }
}
