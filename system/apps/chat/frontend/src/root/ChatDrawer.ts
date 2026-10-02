/**
 * The chats' drawer in the chat root's phone layout: the list (``ChatRail``, rows unchanged)
 * on a panel that slides in from the left over the chat, with a scrim over the rest. On a
 * touchscreen the panel takes most of the width; under a mouse it is as wide as the rail the
 * list draws in a wider window. A tap on
 * the scrim, Escape, or dragging the panel back to the left dismisses it; while a modal is open
 * over the drawer, Escape is the modal's.
 */

import m from "mithril";
import { ChatRail } from "./ChatRail";
import type { ChatRailAttrs } from "./ChatRail";

export interface ChatDrawerAttrs {
  rail: ChatRailAttrs;
  /** Whether a modal is open over the drawer. */
  isCovered: boolean;
  onDismiss: () => void;
}

/** How far the finger travels before a press on the panel is a drag rather than a tap or a scroll. */
const DRAG_SLOP_PX = 8;
/** How far left a drag has to take the panel for its release to dismiss it; less springs it back. */
const DISMISS_FRACTION = 0.3;

export function ChatDrawer(): m.Component<ChatDrawerAttrs> {
  let onDismiss: () => void = () => undefined;
  let isCovered = false;
  let panel: HTMLElement | null = null;
  let scrim: HTMLElement | null = null;
  // The press being followed, from its pointerdown to its release; null between presses.
  let press: { pointerId: number; startX: number; startY: number; offset: number; isDragging: boolean } | null = null;
  // Set when a press ended as a drag, so the click it releases into does not also pick a row.
  let isClickSwallowed = false;

  function place(offset: number, isAnimated: boolean): void {
    if (panel === null) return;
    panel.style.transition = isAnimated ? "transform var(--dur-slow) ease-out" : "none";
    panel.style.transform = offset === 0 ? "" : `translateX(${offset}px)`;
    if (scrim !== null) scrim.style.opacity = String(1 + offset / panel.offsetWidth);
  }

  function onPointerMove(event: PointerEvent): void {
    if (press === null || event.pointerId !== press.pointerId || panel === null) return;
    const dx = event.clientX - press.startX;
    const dy = event.clientY - press.startY;
    if (!press.isDragging) {
      // Only a leftward, mostly horizontal move is the panel's; anything else is the list scrolling or a tap.
      if (Math.abs(dx) < DRAG_SLOP_PX || Math.abs(dx) <= Math.abs(dy) || dx > 0) return;
      press.isDragging = true;
    }
    press.offset = Math.min(0, dx);
    place(press.offset, false);
  }

  function endPress(event: PointerEvent): void {
    if (press === null || event.pointerId !== press.pointerId) return;
    const ended = press;
    press = null;
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", endPress);
    window.removeEventListener("pointercancel", endPress);
    if (!ended.isDragging || panel === null) return;
    isClickSwallowed = true;
    // A click, when one follows the release, comes in the same task; past it there is nothing to swallow.
    window.setTimeout(() => {
      isClickSwallowed = false;
    }, 0);
    if (-ended.offset > panel.offsetWidth * DISMISS_FRACTION) {
      onDismiss();
      m.redraw();
      return;
    }
    place(0, true);
  }

  function onPointerDown(event: PointerEvent): void {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    press = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, offset: 0, isDragging: false };
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", endPress);
    window.addEventListener("pointercancel", endPress);
  }

  function swallowClickAfterDrag(event: MouseEvent): void {
    if (!isClickSwallowed) return;
    event.preventDefault();
    event.stopPropagation();
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key !== "Escape" || isCovered) return;
    onDismiss();
    m.redraw();
  }

  return {
    oncreate({ dom }) {
      window.addEventListener("keydown", onKeydown);
      panel = dom.querySelector<HTMLElement>(".chat-drawer-panel");
      scrim = dom.querySelector<HTMLElement>(".chat-drawer-scrim");
      // Native listeners: a drag moves the panel directly, frame by frame, with no redraw of the root behind it.
      panel?.addEventListener("pointerdown", onPointerDown);
      panel?.addEventListener("click", swallowClickAfterDrag, true);
    },
    onremove() {
      window.removeEventListener("keydown", onKeydown);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", endPress);
      window.removeEventListener("pointercancel", endPress);
      panel = null;
      scrim = null;
      press = null;
    },
    view({ attrs }) {
      onDismiss = attrs.onDismiss;
      isCovered = attrs.isCovered;
      return m("div", { class: "chat-drawer absolute inset-0 z-(--z-sticky)", "data-chat-drawer": "" }, [
        m("div", {
          class: "chat-drawer-scrim absolute inset-0 bg-black/30 animate-[modal-overlay-in_160ms_ease-out]",
          onclick: () => attrs.onDismiss(),
        }),
        m(
          "div",
          {
            class: [
              "chat-drawer-panel absolute top-0 bottom-0 left-0 flex flex-col bg-surface",
              "shadow-[8px_0_32px_rgb(0_0_0/0.18)] animate-[chat-drawer-in_160ms_ease-out] touch-pan-y",
              attrs.rail.isTouch ? "w-[86%] max-w-[340px]" : "",
            ].join(" "),
          },
          m(ChatRail, attrs.rail),
        ),
      ]);
    },
  };
}
