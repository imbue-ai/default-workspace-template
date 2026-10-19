/**
 * The phone's touch gestures over the stage (docs/system/specs/screen/spec.md section 14), classified from
 * pointer events: a tap is a click at the point, a press held still is a right click, one finger moving is a pan
 * (of the view when zoomed in, else a touch drag on the Screen, which is how a page scrolls), two fingers moving
 * together scroll the Screen in wheel steps, two fingers moving apart or together zoom. A tap reaches the Screen as a
 * touch through neko's input driver and a long press as a right click.
 */

import type { Point } from "./view_transform";

export const TAP_MAX_MS = 350;
export const LONG_PRESS_MS = 500;
export const MOVE_THRESHOLD_PX = 8;
/** How far two fingers' distance must change before the gesture is a pinch rather than a scroll. */
export const PINCH_THRESHOLD_PX = 12;
export const SCROLL_STEP_PX = 40;

export interface PointerSample {
  pointerId: number;
  x: number;
  y: number;
  timeMs: number;
}

export type Gesture =
  | { kind: "tap"; at: Point }
  | { kind: "long-press"; at: Point }
  /** One finger moving: where it started, where it is, and how far it moved since the last sample. */
  | { kind: "pan"; start: Point; at: Point; delta: Point }
  /** The finger of a pan lifted (or a second finger joined it), where it was last. */
  | { kind: "pan-end"; at: Point }
  | { kind: "scroll"; steps: Point }
  | { kind: "pinch"; factor: number; about: Point }
  | { kind: "none" };

interface ActivePointer {
  readonly start: PointerSample;
  last: PointerSample;
}

/** Classifies one touch sequence at a time; the caller feeds every pointer event and acts on what comes back. */
export class GestureClassifier {
  private readonly pointers = new Map<number, ActivePointer>();
  private isMoved = false;
  private isLongPressFired = false;
  private isTwoFinger = false;
  private twoFingerMode: "undecided" | "scroll" | "pinch" = "undecided";
  private lastDistance = 0;
  private lastCentre: Point | null = null;
  private scrollRemainder: Point = { x: 0, y: 0 };

  /** How many fingers are down. */
  get pointerCount(): number {
    return this.pointers.size;
  }

  down(sample: PointerSample): Gesture {
    this.pointers.set(sample.pointerId, { start: sample, last: sample });
    if (this.pointers.size === 1) {
      this.isMoved = false;
      this.isLongPressFired = false;
      this.isTwoFinger = false;
      this.twoFingerMode = "undecided";
      this.scrollRemainder = { x: 0, y: 0 };
    } else if (this.pointers.size === 2) {
      // A second finger makes the sequence two-finger for good: no tap or long press comes of it, and a pan the
      // first finger had begun is over.
      const wasPanning = this.isMoved && !this.isTwoFinger;
      const [first] = this.pointers.values();
      this.isTwoFinger = true;
      this.lastDistance = this.distance();
      this.lastCentre = this.centre();
      if (wasPanning) return { kind: "pan-end", at: { x: first.last.x, y: first.last.y } };
    }
    return { kind: "none" };
  }

  move(sample: PointerSample): Gesture {
    const pointer = this.pointers.get(sample.pointerId);
    if (pointer === undefined) return { kind: "none" };
    const previous = pointer.last;
    pointer.last = sample;
    if (this.pointers.size >= 2) return this.twoFingerMove();
    if (this.isTwoFinger || this.isLongPressFired) return { kind: "none" };
    const travelled = Math.hypot(sample.x - pointer.start.x, sample.y - pointer.start.y);
    if (!this.isMoved && travelled < MOVE_THRESHOLD_PX) return { kind: "none" };
    this.isMoved = true;
    return {
      kind: "pan",
      start: { x: pointer.start.x, y: pointer.start.y },
      at: { x: sample.x, y: sample.y },
      delta: { x: sample.x - previous.x, y: sample.y - previous.y },
    };
  }

  up(sample: PointerSample): Gesture {
    const pointer = this.pointers.get(sample.pointerId);
    this.pointers.delete(sample.pointerId);
    if (pointer === undefined) return { kind: "none" };
    if (this.pointers.size > 0) {
      // One finger of two lifted: the rest of the sequence is neither a pan nor a tap.
      this.isTwoFinger = true;
      return { kind: "none" };
    }
    if (this.isTwoFinger || this.isLongPressFired) return { kind: "none" };
    if (this.isMoved) return { kind: "pan-end", at: { x: sample.x, y: sample.y } };
    if (sample.timeMs - pointer.start.timeMs > TAP_MAX_MS) return { kind: "none" };
    return { kind: "tap", at: { x: pointer.start.x, y: pointer.start.y } };
  }

  cancel(pointerId: number): Gesture {
    const pointer = this.pointers.get(pointerId);
    this.pointers.delete(pointerId);
    const wasPanning = pointer !== undefined && this.pointers.size === 0 && this.isMoved && !this.isTwoFinger;
    this.isTwoFinger = this.isTwoFinger || this.pointers.size > 0;
    if (wasPanning) return { kind: "pan-end", at: { x: pointer.last.x, y: pointer.last.y } };
    return { kind: "none" };
  }

  /** The long-press clock fired (the caller schedules it ``LONG_PRESS_MS`` after a down): a right click when one
   *  still finger is down. */
  longPress(): Gesture {
    if (this.pointers.size !== 1 || this.isMoved || this.isTwoFinger || this.isLongPressFired) return { kind: "none" };
    this.isLongPressFired = true;
    const [pointer] = this.pointers.values();
    return { kind: "long-press", at: { x: pointer.start.x, y: pointer.start.y } };
  }

  private twoFingerMove(): Gesture {
    const distance = this.distance();
    const centre = this.centre();
    const previousCentre = this.lastCentre ?? centre;
    this.lastCentre = centre;
    if (this.twoFingerMode === "undecided") {
      if (Math.abs(distance - this.lastDistance) >= PINCH_THRESHOLD_PX) this.twoFingerMode = "pinch";
      else if (Math.hypot(centre.x - previousCentre.x, centre.y - previousCentre.y) >= MOVE_THRESHOLD_PX / 2)
        this.twoFingerMode = "scroll";
      else return { kind: "none" };
    }
    if (this.twoFingerMode === "pinch") {
      const factor = this.lastDistance > 0 ? distance / this.lastDistance : 1;
      this.lastDistance = distance;
      return { kind: "pinch", factor, about: centre };
    }
    this.lastDistance = distance;
    this.scrollRemainder = {
      x: this.scrollRemainder.x + (centre.x - previousCentre.x),
      y: this.scrollRemainder.y + (centre.y - previousCentre.y),
    };
    const steps = {
      x: Math.trunc(this.scrollRemainder.x / SCROLL_STEP_PX),
      y: Math.trunc(this.scrollRemainder.y / SCROLL_STEP_PX),
    };
    if (steps.x === 0 && steps.y === 0) return { kind: "none" };
    this.scrollRemainder = {
      x: this.scrollRemainder.x - steps.x * SCROLL_STEP_PX,
      y: this.scrollRemainder.y - steps.y * SCROLL_STEP_PX,
    };
    // Fingers moving down pull the content down, which is scrolling up: the steps are the wheel's, the opposite of
    // the fingers' travel, in the DOM's sign (the input relay turns that into neko's).
    return { kind: "scroll", steps: { x: -steps.x, y: -steps.y } };
  }

  private distance(): number {
    const [a, b] = [...this.pointers.values()].map((pointer) => pointer.last);
    return Math.hypot(a.x - b.x, a.y - b.y);
  }

  private centre(): Point {
    const [a, b] = [...this.pointers.values()].map((pointer) => pointer.last);
    return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  }
}
