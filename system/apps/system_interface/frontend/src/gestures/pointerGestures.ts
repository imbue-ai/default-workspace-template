/**
 * The one module that listens to pointer events for window drag, resize, and shortcut drag
 * (desktop-interface plan section 6.5). It binds by data attribute (``data-drag-handle`` inside a
 * window, ``data-resize-edge``, ``data-shortcut``), uses pointer capture and a start threshold
 * (a press that never travels the threshold is a click, left to the element's own handlers),
 * and sits behind the ``GestureSource`` interface so interact.js could replace it without
 * touching a reducer. Touch needs nothing extra beyond ``touch-action: none`` on the handles; a
 * long press stands in for the right click.
 */

import type { PixelPoint } from "../geometry/frames";
import { isResizeEdge } from "../geometry/frames";
import type { ResizeEdge } from "../geometry/frames";
import { WINDOW_ID_ATTRIBUTE } from "../pages/livePages";

export const DRAG_HANDLE_ATTRIBUTE = "data-drag-handle";
export const RESIZE_EDGE_ATTRIBUTE = "data-resize-edge";
export const SHORTCUT_ATTRIBUTE = "data-shortcut";
export const TASKBAR_ENTRY_ATTRIBUTE = "data-taskbar-entry";
/** A pinned entry, in the bar or floating; only the floating one (which is no taskbar entry) drags. */
export const PINNED_ENTRY_ATTRIBUTE = "data-pinned-entry";
/** Marks an element (a menu button) whose press must not start a drag. */
export const NO_DRAG_ATTRIBUTE = "data-no-drag";

const LONG_PRESS_MS = 500;
const PRIMARY_BUTTON = 0;

/** What a press landed on, as the data attributes name it. */
export type GestureBinding =
  | { readonly kind: "window-move"; readonly windowId: string }
  | { readonly kind: "window-resize"; readonly windowId: string; readonly edge: ResizeEdge }
  | { readonly kind: "shortcut"; readonly app: string; readonly launch: string; readonly element: HTMLElement }
  /** A taskbar entry never drags, but a long press on it asks for its menu. */
  | { readonly kind: "taskbar-entry"; readonly windowId: string }
  /** A floating pinned entry: a drag moves it, a long press asks for its menu. */
  | { readonly kind: "floating-entry"; readonly app: string; readonly element: HTMLElement };

export interface GestureListener {
  /** The distance a press travels before it is a drag, in pixels. */
  thresholdPx(): number;
  /** Whether a binding may start a drag right now (compact mode turns window drags off). */
  isDraggable(binding: GestureBinding): boolean;
  /** ``point`` is in the root's own coordinates (the backdrop's pixels). */
  onBegin(binding: GestureBinding, point: PixelPoint, pressPoint: PixelPoint): void;
  onMove(binding: GestureBinding, point: PixelPoint, delta: PixelPoint): void;
  onEnd(binding: GestureBinding, point: PixelPoint, delta: PixelPoint): void;
  onCancel(binding: GestureBinding): void;
  /** A press held still past the long-press delay (touch's right click); ``client`` is in viewport coordinates. */
  onLongPress(binding: GestureBinding, client: PixelPoint): void;
}

/** A source of drag gestures over a root element. */
export interface GestureSource {
  /** Start listening on ``root``; answers a function that stops. */
  attach(root: HTMLElement, listener: GestureListener): () => void;
}

/** The binding a press on ``target`` names, or null when it names none. */
export function bindingForTarget(target: Element): GestureBinding | null {
  if (target.closest(`[${NO_DRAG_ATTRIBUTE}]`) !== null) return null;
  const entry = target.closest<HTMLElement>(`[${TASKBAR_ENTRY_ATTRIBUTE}]`);
  if (entry !== null) {
    const windowId = entry.getAttribute(TASKBAR_ENTRY_ATTRIBUTE) ?? "";
    return windowId === "" ? null : { kind: "taskbar-entry", windowId };
  }
  const floating = target.closest<HTMLElement>(`[${PINNED_ENTRY_ATTRIBUTE}]`);
  if (floating !== null) {
    const app = floating.getAttribute(PINNED_ENTRY_ATTRIBUTE) ?? "";
    return app === "" ? null : { kind: "floating-entry", app, element: floating };
  }
  const shortcut = target.closest<HTMLElement>(`[${SHORTCUT_ATTRIBUTE}]`);
  if (shortcut !== null) {
    const key = shortcut.getAttribute(SHORTCUT_ATTRIBUTE) ?? "";
    const separator = key.indexOf(":");
    if (separator <= 0) return null;
    return {
      kind: "shortcut",
      app: key.substring(0, separator),
      launch: key.substring(separator + 1),
      element: shortcut,
    };
  }
  const edge = target.closest<HTMLElement>(`[${RESIZE_EDGE_ATTRIBUTE}]`);
  const windowElement = target.closest<HTMLElement>(`[${WINDOW_ID_ATTRIBUTE}]`);
  const windowId = windowElement?.getAttribute(WINDOW_ID_ATTRIBUTE) ?? null;
  if (windowId === null || windowId === "") return null;
  if (edge !== null) {
    const name = edge.getAttribute(RESIZE_EDGE_ATTRIBUTE) ?? "";
    return isResizeEdge(name) ? { kind: "window-resize", windowId, edge: name } : null;
  }
  if (target.closest(`[${DRAG_HANDLE_ATTRIBUTE}]`) !== null) return { kind: "window-move", windowId };
  return null;
}

interface PendingPress {
  readonly binding: GestureBinding;
  readonly pointerId: number;
  readonly pointerType: string;
  readonly pressClient: PixelPoint;
  readonly press: PixelPoint;
  isDragging: boolean;
  longPressTimer: ReturnType<typeof setTimeout> | null;
}

/** Whether a pointer is the one hovering device (a mouse or a pen) rather than one finger among several. */
function isHoveringDevice(pointerType: string): boolean {
  return pointerType === "mouse" || pointerType === "pen";
}

/** Whether ``event`` continues the pointer ``pending`` pressed with.
 *
 * A finger is its pointer id. A mouse and a pen are the same hand: some X servers report one physical
 * mouse as a mouse for its button and as a pen for its motion (an absolute-axis virtual mouse under a
 * VM does), so a press with one id followed by moves with another must still be one gesture. */
export function isSamePointer(pending: { pointerId: number; pointerType: string }, event: PointerEvent): boolean {
  if (event.pointerId === pending.pointerId) return true;
  return isHoveringDevice(pending.pointerType) && isHoveringDevice(event.pointerType);
}

/** The pointer-event implementation of ``GestureSource``. */
export class PointerGestureSource implements GestureSource {
  attach(root: HTMLElement, listener: GestureListener): () => void {
    let pending: PendingPress | null = null;

    const pointOf = (event: PointerEvent): PixelPoint => {
      const origin = root.getBoundingClientRect();
      return { x: event.clientX - origin.left, y: event.clientY - origin.top };
    };

    const clearLongPress = (): void => {
      if (pending?.longPressTimer != null) clearTimeout(pending.longPressTimer);
      if (pending !== null) pending.longPressTimer = null;
    };

    const finish = (): void => {
      clearLongPress();
      pending = null;
    };

    const onPointerDown = (event: PointerEvent): void => {
      // A new press: the last drag's click has fired by now or never will.
      suppressNextClick = false;
      if (event.button !== PRIMARY_BUTTON) return;
      // A second pointer during a drag is ignored; a press still pending without a drag is stale (it was
      // released over a live page, whose document took the pointerup) and this press replaces it.
      if (pending !== null) {
        if (pending.isDragging) return;
        finish();
      }
      if (!(event.target instanceof Element)) return;
      const binding = bindingForTarget(event.target);
      if (binding === null) return;
      // A press on a handle owns the pointer: left to its default, a press inside an existing text
      // selection starts a native text drag on the first move, and the browser answers that with a
      // pointercancel that kills the gesture. Clicks and double clicks still fire.
      event.preventDefault();
      const press = pointOf(event);
      const pressClient = { x: event.clientX, y: event.clientY };
      pending = {
        binding,
        pointerId: event.pointerId,
        pointerType: event.pointerType,
        press,
        pressClient,
        isDragging: false,
        longPressTimer: null,
      };
      if (event.pointerType !== "mouse") {
        pending.longPressTimer = setTimeout(() => {
          if (pending === null || pending.isDragging) return;
          const held = pending;
          // The press is spent on the menu: the click its release fires must not run the element too.
          suppressNextClick = true;
          finish();
          listener.onLongPress(held.binding, held.pressClient);
        }, LONG_PRESS_MS);
      }
    };

    const onPointerMove = (event: PointerEvent): void => {
      if (pending === null || !isSamePointer(pending, event)) return;
      // No button held: the press ended where the root could not see it (over a live page, or while the
      // window had lost focus); the pointer is only hovering now. A drag that had begun is cancelled, so
      // the listener's begin is always answered by an end or a cancel.
      if (event.buttons === 0) {
        const held = pending;
        finish();
        if (held.isDragging) listener.onCancel(held.binding);
        return;
      }
      const point = pointOf(event);
      const delta = { x: point.x - pending.press.x, y: point.y - pending.press.y };
      if (!pending.isDragging) {
        if (Math.hypot(delta.x, delta.y) < listener.thresholdPx()) return;
        if (!listener.isDraggable(pending.binding)) {
          finish();
          return;
        }
        pending.isDragging = true;
        clearLongPress();
        // Captured only now: capturing on the press would retarget the click a plain press ends in. The
        // moving event's id, which is the one the browser routes; a device that reports the press under
        // another id has no active pointer there to capture, which the browser refuses.
        try {
          root.setPointerCapture(event.pointerId);
        } catch {
          // The pages are inert for the gesture, so the root still sees the moves uncaptured.
        }
        listener.onBegin(pending.binding, point, pending.press);
      }
      event.preventDefault();
      listener.onMove(pending.binding, point, delta);
    };

    const onPointerUp = (event: PointerEvent): void => {
      if (pending === null || !isSamePointer(pending, event)) return;
      const held = pending;
      const point = pointOf(event);
      finish();
      if (held.isDragging)
        listener.onEnd(held.binding, point, { x: point.x - held.press.x, y: point.y - held.press.y });
    };

    const onPointerCancel = (event: PointerEvent): void => {
      if (pending === null || !isSamePointer(pending, event)) return;
      const held = pending;
      finish();
      if (held.isDragging) listener.onCancel(held.binding);
    };

    // A drag that began, or a long press, suppresses the click a release would otherwise fire on the handle.
    const onClick = (event: MouseEvent): void => {
      if (suppressNextClick) {
        suppressNextClick = false;
        event.stopPropagation();
        event.preventDefault();
      }
    };
    let suppressNextClick = false;
    const markDragEnded = (event: PointerEvent): void => {
      if (pending !== null && pending.isDragging && isSamePointer(pending, event)) suppressNextClick = true;
    };

    root.addEventListener("pointerdown", onPointerDown);
    root.addEventListener("pointermove", onPointerMove);
    root.addEventListener("pointerup", markDragEnded, true);
    root.addEventListener("pointerup", onPointerUp);
    root.addEventListener("pointercancel", onPointerCancel);
    root.addEventListener("click", onClick, true);
    return () => {
      root.removeEventListener("pointerdown", onPointerDown);
      root.removeEventListener("pointermove", onPointerMove);
      root.removeEventListener("pointerup", markDragEnded, true);
      root.removeEventListener("pointerup", onPointerUp);
      root.removeEventListener("pointercancel", onPointerCancel);
      root.removeEventListener("click", onClick, true);
      finish();
    };
  }
}
