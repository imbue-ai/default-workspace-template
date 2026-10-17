/** Human input to neko over its socket: pointer moves and buttons in Screen coordinates, wheel steps, and keys as
 *  X11 keysyms through the Guacamole keyboard. Sent only while this viewer's session is the controller (spec
 *  section 11). */

import GuacamoleKeyboard from "@imbue/workspace-ui/src/guacamole_keyboard.js";
import type { NekoSocket } from "./neko_socket";

const MOVE_COALESCE_MS = 16;
const WHEEL_STEP_PX = 53;

export const XK_CONTROL_L = 0xffe3;
export const XK_W = 0x0077;

export interface ScreenGeometry {
  width: number;
  height: number;
}

/** Where a pointer event lands on the Screen, given the letterboxed video inside the stage; null outside it. */
export function screenPointFromStage(
  clientX: number,
  clientY: number,
  stageRect: DOMRect,
  screen: ScreenGeometry,
): { x: number; y: number } | null {
  if (screen.width <= 0 || screen.height <= 0 || stageRect.width <= 0 || stageRect.height <= 0) return null;
  const scale = Math.min(stageRect.width / screen.width, stageRect.height / screen.height);
  const drawnWidth = screen.width * scale;
  const drawnHeight = screen.height * scale;
  const left = stageRect.left + (stageRect.width - drawnWidth) / 2;
  const top = stageRect.top + (stageRect.height - drawnHeight) / 2;
  const x = Math.round((clientX - left) / scale);
  const y = Math.round((clientY - top) / scale);
  if (x < 0 || y < 0 || x >= screen.width || y >= screen.height) return null;
  return { x, y };
}

export class InputRelay {
  private readonly keyboard = new GuacamoleKeyboard();
  private pendingMove: { x: number; y: number } | null = null;
  private moveTimer = 0;
  private wheel = { x: 0, y: 0, t: 0 };
  isEnabled = false;
  screen: ScreenGeometry = { width: 0, height: 0 };

  constructor(
    private readonly socket: NekoSocket,
    private readonly stage: HTMLElement,
  ) {
    this.keyboard.onkeydown = (keysym) => {
      if (!this.isEnabled) return true;
      this.flushMove();
      socket.send("control/keydown", { keysym });
      return false;
    };
    this.keyboard.onkeyup = (keysym) => {
      if (!this.isEnabled) return;
      socket.send("control/keyup", { keysym });
    };
    this.keyboard.listenTo(stage);
    stage.addEventListener("mousemove", (event) => this.onMove(event));
    stage.addEventListener("mousedown", (event) => this.onButton(event, true));
    stage.addEventListener("mouseup", (event) => this.onButton(event, false));
    stage.addEventListener("contextmenu", (event) => {
      if (this.isEnabled) event.preventDefault();
    });
    stage.addEventListener("wheel", (event) => this.onWheel(event), { passive: false });
    stage.addEventListener("blur", () => this.keyboard.reset());
  }

  setEnabled(isEnabled: boolean): void {
    if (this.isEnabled && !isEnabled) this.keyboard.reset();
    this.isEnabled = isEnabled;
  }

  /** The close chord: Ctrl+W to the remote rather than the window closing (spec section 11). */
  sendCloseChord(): void {
    if (!this.isEnabled) return;
    this.socket.send("control/keydown", { keysym: XK_CONTROL_L });
    this.socket.send("control/keydown", { keysym: XK_W });
    this.socket.send("control/keyup", { keysym: XK_W });
    this.socket.send("control/keyup", { keysym: XK_CONTROL_L });
  }

  private pointOf(event: MouseEvent): { x: number; y: number } | null {
    return screenPointFromStage(event.clientX, event.clientY, this.stage.getBoundingClientRect(), this.screen);
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
    }
  }
}
