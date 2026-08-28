/**
 * The workspace's tooltip: a single bubble on ``document.body`` rather than
 * next to its target, positioned (fixed) under the target after a hover-intent
 * delay.
 *
 * Tab content in dockview can use neither of the usual tooltip mechanisms.
 * Native ``title`` is suppressed: dockview marks every tab ``draggable``
 * (tab.js sets ``element.draggable = true``, plus
 * ``-webkit-user-drag: element``), and Chromium hides ``title`` tooltips on
 * draggable elements and their descendants. A CSS ``::after`` bubble is
 * clipped by the tab strip's overflow (``.dv-tabs-container`` is
 * ``overflow: auto`` and ``.dv-groupview`` is ``overflow: hidden``). A
 * body-level, fixed-position element driven by our own listeners avoids both:
 * it is not a native tooltip, and it is not inside the clipping container.
 *
 * That being the only mechanism that works everywhere in the workspace, it is
 * the one every workspace tooltip uses: 250ms hover-intent delay, keyboard
 * focus too, no fade, centered under the trigger with a 6px gap, flipped above
 * on bottom overflow, clamped to the viewport, dropped on leave / blur /
 * click / scroll / resize. The centered-below placement is the default
 * everywhere and callers should not opt out of it lightly -- one placement is
 * what keeps every tooltip in the workspace reading as the same tooltip.
 *
 * The one deliberate exception is the project rail: a rail row sits directly
 * above the row it is being compared against (e.g. the shortcut a hover is
 * about to reveal versus the one below it), so a centered-below bubble covers
 * exactly the row the tooltip is meant to help someone choose. ``placeTooltip``
 * takes an optional ``placement`` for that one case, defaulting to the shared
 * centered-below behavior everywhere else.
 */

import type m from "mithril";

/** Hover-intent delay before a tooltip appears. */
const TOOLTIP_DELAY_MS = 250;
/** Gap between the trigger and the bubble. */
const TOOLTIP_GAP = 6;
/** Minimum gap from the window edges. */
const TOOLTIP_MARGIN = 6;

/** The bubble's skin, as design-system utilities (`hover-tooltip` is a bare
 * marker for tests and devtools, with no CSS attached). One literal string so
 * Tailwind's source scan sees every class. `hidden` is the resting state --
 * ``showBubble`` toggles `display` inline -- and there is deliberately no
 * transition. The colour tokens don't flip with the scheme, so dark mode is
 * spelled out as `dark:` variants off `prefers-color-scheme`. `z-(--z-tooltip)`
 * clears the modal overlays. */
const TOOLTIP_CLASS =
  "hover-tooltip type-helper pointer-events-none fixed z-(--z-tooltip) hidden max-w-[480px] items-center gap-1.5 rounded-md bg-inverse px-2 py-1 text-center whitespace-normal text-on-accent shadow-overlay dark:bg-surface dark:text-primary";

export interface HoverTooltip {
  /** Set the text shown on hover, or ``null`` to disable the tooltip. */
  setText(text: string | null): void;
  /** Remove listeners and any visible bubble. */
  dispose(): void;
}

/** The part of a ``DOMRect`` the placement needs. */
export interface TooltipAnchor {
  left: number;
  top: number;
  bottom: number;
  width: number;
}

export interface TooltipSize {
  width: number;
  height: number;
}

export interface TooltipPosition {
  left: number;
  top: number;
}

/**
 * ``"below"`` (the default everywhere) centers the bubble under the trigger.
 * ``"right"`` is the rail's exception -- see the module doc comment -- and
 * places the bubble beside the trigger instead, so it never covers the row
 * underneath.
 */
export type TooltipPlacement = "below" | "right";

/**
 * Where the bubble goes for the default ``"below"`` placement: centered under
 * the trigger with a ``TOOLTIP_GAP`` gap, flipped above when it would
 * otherwise overflow the bottom (and there is room up there), then clamped
 * ``TOOLTIP_MARGIN`` from the viewport edges.
 */
function placeTooltipBelow(anchor: TooltipAnchor, bubble: TooltipSize, viewport: TooltipSize): TooltipPosition {
  const centered = anchor.left + anchor.width / 2 - bubble.width / 2;
  const below = anchor.bottom + TOOLTIP_GAP;
  const above = anchor.top - bubble.height - TOOLTIP_GAP;
  const overflowsBottom = below + bubble.height > viewport.height - TOOLTIP_MARGIN;
  const top = overflowsBottom && above >= TOOLTIP_MARGIN ? above : below;
  return {
    left: Math.max(TOOLTIP_MARGIN, Math.min(centered, viewport.width - TOOLTIP_MARGIN - bubble.width)),
    top: Math.max(TOOLTIP_MARGIN, top),
  };
}

/**
 * Where the bubble goes for ``"right"`` placement: vertically centered on the
 * trigger, offset ``TOOLTIP_GAP`` to its right, flipped to the trigger's left
 * when it would otherwise overflow the right edge (and there is room over
 * there). Unlike the below/above flip, both axes get the full min-and-max
 * clamp here rather than only the near-edge one: a rail row's tooltip must
 * never run off the right edge of the viewport (the one direction "below"
 * tolerates overflowing, in the pathological case of a trigger taller than
 * the viewport, has no equivalent excuse on this axis), so this clamps hard
 * both ways instead of reproducing that allowance.
 */
function placeTooltipRight(anchor: TooltipAnchor, bubble: TooltipSize, viewport: TooltipSize): TooltipPosition {
  const verticalCenter = anchor.top + (anchor.bottom - anchor.top) / 2 - bubble.height / 2;
  const right = anchor.left + anchor.width + TOOLTIP_GAP;
  const left = anchor.left - bubble.width - TOOLTIP_GAP;
  const overflowsRight = right + bubble.width > viewport.width - TOOLTIP_MARGIN;
  const preferredLeft = overflowsRight && left >= TOOLTIP_MARGIN ? left : right;
  return {
    left: Math.max(TOOLTIP_MARGIN, Math.min(preferredLeft, viewport.width - TOOLTIP_MARGIN - bubble.width)),
    top: Math.max(TOOLTIP_MARGIN, Math.min(verticalCenter, viewport.height - TOOLTIP_MARGIN - bubble.height)),
  };
}

/**
 * Where the bubble goes, given where the trigger and the bubble itself are.
 * ``placement`` defaults to ``"below"`` -- the shared, shell-matched
 * behavior every caller gets unless it explicitly asks for ``"right"``.
 */
export function placeTooltip(
  anchor: TooltipAnchor,
  bubble: TooltipSize,
  viewport: TooltipSize,
  placement: TooltipPlacement = "below",
): TooltipPosition {
  switch (placement) {
    case "below":
      return placeTooltipBelow(anchor, bubble, viewport);
    case "right":
      return placeTooltipRight(anchor, bubble, viewport);
  }
}

// One bubble is enough: only one tooltip is ever visible, so every trigger
// shares it. ``pendingFor`` and ``shownFor`` name the trigger a scheduled or
// visible bubble belongs to, so a trigger only ever dismisses its own.
let bubbleElement: HTMLDivElement | null = null;
let pendingTimer: number | null = null;
let pendingFor: Element | null = null;
let shownFor: Element | null = null;
let isWindowWired = false;

function ensureBubble(): HTMLDivElement {
  if (bubbleElement === null) {
    bubbleElement = document.createElement("div");
    bubbleElement.className = TOOLTIP_CLASS;
    bubbleElement.setAttribute("role", "tooltip");
    document.body.appendChild(bubbleElement);
  }
  return bubbleElement;
}

function cancelPending(): void {
  if (pendingTimer !== null) {
    window.clearTimeout(pendingTimer);
    pendingTimer = null;
  }
  pendingFor = null;
}

function hideBubble(): void {
  if (bubbleElement !== null) {
    bubbleElement.style.display = "none";
  }
  shownFor = null;
}

/** Drop the tooltip whoever it belongs to -- what the window-level events do. */
function dropTooltip(): void {
  cancelPending();
  hideBubble();
}

function showBubble(target: Element, text: string, placement: TooltipPlacement): void {
  const element = ensureBubble();
  element.textContent = text;
  // Measure at the natural width: clear the width a previous show fixed, and
  // park the bubble at the origin first, since a stale ``left`` would cap the
  // shrink-to-fit width at (viewport - left) and wrap the label.
  element.style.width = "";
  element.style.left = "0";
  element.style.top = "0";
  element.style.visibility = "hidden";
  element.style.display = "inline-flex";
  // getBoundingClientRect, not offsetWidth: offsetWidth rounds the shrink-to-fit
  // width DOWN (e.g. 132.4 -> 132), and fixing the width to that leaves the
  // content a fraction short and wraps the last word. Ceil instead.
  const measured = element.getBoundingClientRect();
  const size = { width: Math.ceil(measured.width), height: Math.ceil(measured.height) };
  const position = placeTooltip(
    target.getBoundingClientRect(),
    size,
    { width: window.innerWidth, height: window.innerHeight },
    placement,
  );
  // Fix the width so the bubble does not reflow if the viewport later changes.
  element.style.width = `${size.width}px`;
  element.style.left = `${position.left}px`;
  element.style.top = `${position.top}px`;
  element.style.visibility = "visible";
  shownFor = target;
}

function wireWindowListeners(): void {
  if (isWindowWired) {
    return;
  }
  isWindowWired = true;
  // Any scroll (capture, so nested scrollers count), resize or window blur
  // slides the trigger out from under a shown bubble, so drop it.
  window.addEventListener("scroll", dropTooltip, true);
  window.addEventListener("resize", dropTooltip);
  window.addEventListener("blur", dropTooltip);
}

/**
 * ``placement`` is fixed for the lifetime of the attachment (unlike the text,
 * it is not expected to change), and defaults to the shared centered-below
 * behavior -- see the module doc comment for the rail's ``"right"`` exception.
 */
export function attachHoverTooltip(target: Element, placement: TooltipPlacement = "below"): HoverTooltip {
  let text: string | null = null;

  wireWindowListeners();

  const dismiss = (): void => {
    if (pendingFor === target) {
      cancelPending();
    }
    if (shownFor === target) {
      hideBubble();
    }
  };

  // Both entry points into a visible bubble: the delay elapsing, and keyboard
  // focus, which skips the delay. Either way whatever else was queued loses.
  const showNow = (): void => {
    cancelPending();
    if (text !== null) {
      showBubble(target, text, placement);
    }
  };

  const onEnter = (): void => {
    if (text === null) {
      return;
    }
    cancelPending();
    pendingFor = target;
    pendingTimer = window.setTimeout(showNow, TOOLTIP_DELAY_MS);
  };

  // Keyboard focus only -- not focus that came from a mouse click, which would
  // flash the tooltip and then immediately hide it on the click.
  const onFocus = (): void => {
    if (target.matches(":focus-visible")) {
      showNow();
    }
  };

  target.addEventListener("mouseenter", onEnter);
  target.addEventListener("mouseleave", dismiss);
  target.addEventListener("click", dismiss);
  target.addEventListener("focus", onFocus);
  target.addEventListener("blur", dismiss);

  return {
    setText(next: string | null): void {
      text = next;
      if (text === null) {
        dismiss();
      } else if (shownFor === target) {
        showBubble(target, text, placement);
      }
    },
    dispose(): void {
      target.removeEventListener("mouseenter", onEnter);
      target.removeEventListener("mouseleave", dismiss);
      target.removeEventListener("click", dismiss);
      target.removeEventListener("focus", onFocus);
      target.removeEventListener("blur", dismiss);
      dismiss();
    },
  };
}

const tooltipsByElement = new WeakMap<Element, HoverTooltip>();

/**
 * The mithril form: spread into an element's attrs in place of a native
 * ``title``, e.g. ``m("button", { onclick, ...hoverTooltipAttrs("Close") })``.
 * The element keeps its own ``aria-label`` -- the bubble is decoration, not an
 * accessible name. ``placement`` defaults to the shared centered-below
 * behavior; pass ``"right"`` only for the rail's exception (see the module
 * doc comment).
 */
export function hoverTooltipAttrs(text: string, placement: TooltipPlacement = "below"): m.Attributes {
  return {
    oncreate: (vnode: m.VnodeDOM): void => {
      const tooltip = attachHoverTooltip(vnode.dom, placement);
      tooltip.setText(text);
      tooltipsByElement.set(vnode.dom, tooltip);
    },
    onupdate: (vnode: m.VnodeDOM): void => {
      tooltipsByElement.get(vnode.dom)?.setText(text);
    },
    onremove: (vnode: m.VnodeDOM): void => {
      tooltipsByElement.get(vnode.dom)?.dispose();
      tooltipsByElement.delete(vnode.dom);
    },
  };
}
