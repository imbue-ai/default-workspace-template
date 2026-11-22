/**
 * A coach mark: a small bubble with an arrow, one line or two of text, and a "Got it" button, pointing at an
 * element of the page that draws it. It says what a control is for the first time the user is led to it.
 *
 * The bubble lives on ``document.body``, as the tooltip does, so no container of the control clips it. It sits
 * beside the control (below, above, right, or left, whichever has room), follows it when the page is resized,
 * scrolled or redrawn, and goes when "Got it" is pressed, when the control itself is clicked, or when the control
 * leaves the page. One coach mark is up at a time: showing another takes the first one down.
 *
 * A page can only point at its own elements. A control that belongs to another layer (the Imbue Studio chrome's,
 * the shell's from inside an app) is that layer's to point at.
 */

import m from "mithril";
import { Button } from "./Button";

/** Gap between the control and the bubble, which the arrow spans. */
const COACH_MARK_GAP = 10;
/** Minimum gap from the window edges. */
const COACH_MARK_MARGIN = 8;
/** How close to a corner of the bubble the arrow's centre may come. */
const ARROW_INSET = 14;

const BUBBLE_CLASS =
  "coach-mark type-body pointer-events-auto fixed z-(--z-popover) flex max-w-[280px] flex-col items-start gap-2 rounded-lg border border-default bg-surface px-3 py-2.5 text-primary shadow-overlay";

// The arrow is a square turned on its corner, half out of the bubble's edge, bordered on the two sides that show.
const ARROW_CLASS_BY_SIDE: Record<CoachMarkSide, string> = {
  below: "coach-mark-arrow absolute size-2.5 rotate-45 border-default bg-surface border-t border-l",
  above: "coach-mark-arrow absolute size-2.5 rotate-45 border-default bg-surface border-b border-r",
  right: "coach-mark-arrow absolute size-2.5 rotate-45 border-default bg-surface border-b border-l",
  left: "coach-mark-arrow absolute size-2.5 rotate-45 border-default bg-surface border-t border-r",
};
/** Half the arrow's box, which is how far it sticks out of the bubble. */
const ARROW_HALF = 5;

export type CoachMarkSide = "below" | "above" | "right" | "left";

/** The part of a ``DOMRect`` the placement needs. */
export interface CoachMarkAnchor {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface CoachMarkSize {
  width: number;
  height: number;
}

export interface CoachMarkPlacement {
  /** The side of the control the bubble sits on. */
  side: CoachMarkSide;
  left: number;
  top: number;
  /** Where the arrow's centre is along the bubble's edge that faces the control, from that edge's start. */
  arrowOffset: number;
}

const SIDES: readonly CoachMarkSide[] = ["below", "above", "right", "left"];

function clamp(value: number, low: number, high: number): number {
  return Math.max(low, Math.min(value, Math.max(low, high)));
}

interface SidePlacement {
  placement: CoachMarkPlacement;
  /** Whether the bubble is wholly inside the viewport's margins there. */
  fits: boolean;
}

function placeOn(
  side: CoachMarkSide,
  anchor: CoachMarkAnchor,
  bubble: CoachMarkSize,
  viewport: CoachMarkSize,
): SidePlacement {
  const centerX = anchor.left + anchor.width / 2;
  const centerY = anchor.top + anchor.height / 2;
  const maxLeft = viewport.width - COACH_MARK_MARGIN - bubble.width;
  const maxTop = viewport.height - COACH_MARK_MARGIN - bubble.height;
  if (side === "below" || side === "above") {
    const top =
      side === "below" ? anchor.top + anchor.height + COACH_MARK_GAP : anchor.top - COACH_MARK_GAP - bubble.height;
    const left = clamp(centerX - bubble.width / 2, COACH_MARK_MARGIN, maxLeft);
    const arrowOffset = clamp(centerX - left, ARROW_INSET, bubble.width - ARROW_INSET);
    return { placement: { side, left, top, arrowOffset }, fits: top >= COACH_MARK_MARGIN && top <= maxTop };
  }
  const left =
    side === "right" ? anchor.left + anchor.width + COACH_MARK_GAP : anchor.left - COACH_MARK_GAP - bubble.width;
  const top = clamp(centerY - bubble.height / 2, COACH_MARK_MARGIN, maxTop);
  const arrowOffset = clamp(centerY - top, ARROW_INSET, bubble.height - ARROW_INSET);
  return { placement: { side, left, top, arrowOffset }, fits: left >= COACH_MARK_MARGIN && left <= maxLeft };
}

/**
 * Where the bubble goes: on the first of below, above, right and left of the control that has room for it whole,
 * centred on the control along the other axis and kept ``COACH_MARK_MARGIN`` inside the viewport. With room on no
 * side it goes below, held inside the viewport as far as that is possible.
 */
export function placeCoachMark(
  anchor: CoachMarkAnchor,
  bubble: CoachMarkSize,
  viewport: CoachMarkSize,
): CoachMarkPlacement {
  for (const side of SIDES) {
    const candidate = placeOn(side, anchor, bubble, viewport);
    if (candidate.fits) return candidate.placement;
  }
  const below = placeOn("below", anchor, bubble, viewport).placement;
  return { ...below, top: clamp(below.top, COACH_MARK_MARGIN, viewport.height - COACH_MARK_MARGIN - bubble.height) };
}

export interface CoachMarkOptions {
  /** The control the bubble points at. */
  anchor: HTMLElement;
  text: string;
  /** Called once when the bubble goes, whatever took it down. */
  onDismiss?: () => void;
  /** A class the control wears while the bubble is up, for a page that draws attention to it with an animation. */
  pulseClass?: string;
}

export interface CoachMarkHandle {
  /** Take the bubble down; nothing happens when it is already down. */
  dismiss: () => void;
}

let shown: CoachMarkHandle | null = null;

/** Put the arrow on the bubble's edge that faces the control, its centre ``arrowOffset`` along that edge. */
function positionArrow(arrow: HTMLElement, placement: CoachMarkPlacement): void {
  arrow.className = ARROW_CLASS_BY_SIDE[placement.side];
  const along = `${placement.arrowOffset - ARROW_HALF}px`;
  const out = `${-ARROW_HALF - 1}px`;
  arrow.style.cssText = "";
  switch (placement.side) {
    case "below":
      arrow.style.top = out;
      arrow.style.left = along;
      break;
    case "above":
      arrow.style.bottom = out;
      arrow.style.left = along;
      break;
    case "right":
      arrow.style.left = out;
      arrow.style.top = along;
      break;
    case "left":
      arrow.style.right = out;
      arrow.style.top = along;
      break;
  }
}

/** Point a coach mark at ``options.anchor``, taking down any other one that is up. */
export function showCoachMark(options: CoachMarkOptions): CoachMarkHandle {
  shown?.dismiss();
  const { anchor, text, onDismiss, pulseClass } = options;
  let isUp = true;
  let pendingFrame: number | null = null;

  const bubble = document.createElement("div");
  bubble.className = BUBBLE_CLASS;
  bubble.setAttribute("role", "dialog");
  bubble.setAttribute("aria-label", text);
  const arrow = document.createElement("span");
  arrow.setAttribute("aria-hidden", "true");
  const body = document.createElement("div");
  body.className = "contents";
  bubble.append(arrow, body);

  function dismiss(): void {
    if (!isUp) return;
    isUp = false;
    if (pendingFrame !== null) cancelAnimationFrame(pendingFrame);
    observer.disconnect();
    window.removeEventListener("resize", schedulePlace);
    window.removeEventListener("scroll", schedulePlace, true);
    anchor.removeEventListener("click", dismiss);
    if (pulseClass !== undefined) anchor.classList.remove(pulseClass);
    m.render(body, null);
    bubble.remove();
    if (shown === handle) shown = null;
    onDismiss?.();
  }

  function place(): void {
    pendingFrame = null;
    if (!isUp) return;
    if (!anchor.isConnected) {
      dismiss();
      return;
    }
    // Measured at its natural size, from the origin, where no earlier position can squeeze it.
    bubble.style.left = "0";
    bubble.style.top = "0";
    const measured = bubble.getBoundingClientRect();
    const placement = placeCoachMark(
      anchor.getBoundingClientRect(),
      { width: Math.ceil(measured.width), height: Math.ceil(measured.height) },
      { width: window.innerWidth, height: window.innerHeight },
    );
    bubble.style.left = `${placement.left}px`;
    bubble.style.top = `${placement.top}px`;
    bubble.dataset.side = placement.side;
    positionArrow(arrow, placement);
  }

  function schedulePlace(): void {
    if (pendingFrame === null) pendingFrame = requestAnimationFrame(place);
  }

  // The control can move or go with any redraw of the page, which is no event of its own. The bubble's own
  // repositioning is a mutation too, and is left out so it cannot schedule itself forever.
  const observer = new MutationObserver((records) => {
    if (records.some((record) => !bubble.contains(record.target))) schedulePlace();
  });

  const handle: CoachMarkHandle = { dismiss };
  shown = handle;
  if (pulseClass !== undefined) anchor.classList.add(pulseClass);
  m.render(body, [
    m("span", { class: "coach-mark-text" }, text),
    m(Button, { variant: "secondary", sm: true, extra: "coach-mark-dismiss self-end", onclick: dismiss }, "Got it"),
  ]);
  document.body.appendChild(bubble);
  place();
  observer.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["class", "style"],
  });
  window.addEventListener("resize", schedulePlace);
  window.addEventListener("scroll", schedulePlace, true);
  anchor.addEventListener("click", dismiss);
  return handle;
}
