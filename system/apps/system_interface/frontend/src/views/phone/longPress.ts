/**
 * A tap that can also be held (plan-phone-interface.md): the attributes to spread onto the element. A press
 * held still for the long-press time runs ``onLongPress`` and swallows the click that follows its release; a
 * press that moves past the drag threshold is a scroll and runs neither. The browser's own long-press menu is
 * kept off the element.
 *
 * The press is kept by element rather than in the attributes' closure: mithril redraws after every handler and
 * spreads fresh attributes, so a closure's timer would be lost between the press and its release.
 */

import type m from "mithril";
import { LONG_PRESS_MS } from "../../gestures/pointerGestures";

export interface LongPressHandlers {
  readonly onTap: () => void;
  readonly onLongPress: (target: HTMLElement) => void;
  /** How far a press may wander before it stops being one, in px. */
  readonly thresholdPx: number;
}

interface Press {
  timer: ReturnType<typeof setTimeout> | null;
  readonly x: number;
  readonly y: number;
  isHeld: boolean;
}

const presses = new WeakMap<Element, Press>();

function cancelTimer(element: Element): void {
  const press = presses.get(element);
  if (press?.timer != null) clearTimeout(press.timer);
  if (press !== undefined) press.timer = null;
}

export function longPressAttrs(handlers: LongPressHandlers): m.Attributes {
  return {
    onpointerdown: (event: PointerEvent) => {
      if (event.button !== 0) return;
      const target = event.currentTarget as HTMLElement;
      cancelTimer(target);
      const press: Press = { timer: null, x: event.clientX, y: event.clientY, isHeld: false };
      press.timer = setTimeout(() => {
        press.timer = null;
        press.isHeld = true;
        handlers.onLongPress(target);
      }, LONG_PRESS_MS);
      presses.set(target, press);
    },
    onpointermove: (event: PointerEvent) => {
      const press = presses.get(event.currentTarget as Element);
      if (press === undefined || press.timer === null) return;
      if (Math.hypot(event.clientX - press.x, event.clientY - press.y) > handlers.thresholdPx) {
        cancelTimer(event.currentTarget as Element);
      }
    },
    onpointerup: (event: PointerEvent) => cancelTimer(event.currentTarget as Element),
    onpointercancel: (event: PointerEvent) => cancelTimer(event.currentTarget as Element),
    onpointerleave: (event: PointerEvent) => cancelTimer(event.currentTarget as Element),
    oncontextmenu: (event: Event) => event.preventDefault(),
    onclick: (event: MouseEvent) => {
      const press = presses.get(event.currentTarget as Element);
      if (press?.isHeld === true) {
        press.isHeld = false;
        event.preventDefault();
        return;
      }
      handlers.onTap();
    },
  };
}
