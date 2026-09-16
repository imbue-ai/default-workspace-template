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
 * on bottom overflow, clamped to the viewport, dropped when its own trigger is
 * left or blurred and on any click / scroll / resize. The centered-below
 * placement is the default everywhere and callers should not opt out of it
 * lightly -- one placement is what keeps every tooltip in the workspace
 * reading as the same tooltip.
 *
 * **A trigger's whole state is its ``data-hover-tooltip`` attribute**, read at
 * the moment the bubble goes up, with one set of listeners on the document
 * serving every tooltip in the workspace. Nothing is attached per element, so
 * there is nothing to strand: an element stops having a tooltip exactly when
 * the attribute goes away, whether a view passes ``null``, stops spreading the
 * attrs, or is patched over by a different vnode that never had them. The
 * bubble is also re-read while it is up, so a trigger that loses its text or
 * leaves the document takes the bubble with it. A trigger must be IN the
 * document to be heard -- a detached tree never reaches the listeners.
 *
 * The exceptions are both the same problem: a bubble under the trigger covers
 * the thing the pointer is choosing between. The project rail takes ``right``,
 * because a rail row sits directly above the row it is being compared against.
 * A list of rows whose own controls raise the tooltips -- a menu, a flyout --
 * takes ``above`` for the same reason, one axis over: the row under the pointer
 * is the one being acted on, and the rows below it are what a bubble would
 * cover. ``placeTooltip`` takes an optional ``placement`` for those, defaulting
 * to the shared centered-below behavior everywhere else.
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
export type TooltipPlacement = "below" | "above" | "right";

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
 * Where the bubble goes for ``"above"`` placement: the mirror of the default --
 * centered OVER the trigger with the same gap, flipped below when it would
 * otherwise overflow the top (and there is room down there), then clamped.
 */
function placeTooltipAbove(anchor: TooltipAnchor, bubble: TooltipSize, viewport: TooltipSize): TooltipPosition {
  const centered = anchor.left + anchor.width / 2 - bubble.width / 2;
  const above = anchor.top - bubble.height - TOOLTIP_GAP;
  const below = anchor.bottom + TOOLTIP_GAP;
  const overflowsTop = above < TOOLTIP_MARGIN;
  const fitsBelow = below + bubble.height <= viewport.height - TOOLTIP_MARGIN;
  const top = overflowsTop && fitsBelow ? below : above;
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
 * never run off the right edge of the viewport.
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
    case "above":
      return placeTooltipAbove(anchor, bubble, viewport);
    case "right":
      return placeTooltipRight(anchor, bubble, viewport);
  }
}

/** The attribute a trigger carries its text in, and the one it names a
 *  non-default placement in. */
const TOOLTIP_ATTR = "data-hover-tooltip";
const PLACEMENT_ATTR = "data-hover-tooltip-placement";

// One bubble is enough: only one tooltip is ever visible, so every trigger
// shares it. ``pendingFor`` and ``shownFor`` name the trigger a scheduled or
// visible bubble belongs to, so a trigger only ever dismisses its own.
let bubbleElement: HTMLDivElement | null = null;
let pendingTimer: number | null = null;
let pendingFor: Element | null = null;
let shownFor: Element | null = null;
let isWired = false;
// Watches a SHOWN bubble's trigger, and only then: the trigger can lose its
// text or leave the document while the bubble is up, and neither fires an
// event of its own.
let shownObserver: MutationObserver | null = null;

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
  shownObserver?.disconnect();
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
  watchShownTrigger(target);
}

/** The text a trigger is offering right now, or null when it is offering none.
 *  Read at every show: the DOM is the only copy, so a bubble cannot outlive the
 *  text that put it up. */
function tooltipTextOf(element: Element): string | null {
  return element.isConnected ? element.getAttribute(TOOLTIP_ATTR) : null;
}

function placementOf(element: Element): TooltipPlacement {
  return element.getAttribute(PLACEMENT_ATTR) === "right" ? "right" : "below";
}

/** Keep a shown bubble honest: its trigger can lose its text or be torn out of
 *  the document while the pointer still rests on it, and neither is an event. */
function watchShownTrigger(target: Element): void {
  shownObserver ??= new MutationObserver(() => {
    if (shownFor === null) {
      return;
    }
    const text = tooltipTextOf(shownFor);
    if (text === null) {
      hideBubble();
    } else if (bubbleElement?.textContent !== text) {
      showBubble(shownFor, text, placementOf(shownFor));
    }
  });
  shownObserver.disconnect();
  // The text is watched on the trigger itself; only the leaves-the-document
  // check needs the whole tree, and that is childList alone -- watching every
  // element's attributes would put a record on every unrelated DOM write while
  // a bubble happens to be up.
  shownObserver.observe(target, { attributeFilter: [TOOLTIP_ATTR] });
  shownObserver.observe(document.body, { childList: true, subtree: true });
}

/** The trigger an event happened inside, or null when it happened outside every
 *  trigger. ``closest``, so the whole subtree of a trigger counts as the
 *  trigger -- a hover over a button's icon is a hover over the button. */
function triggerOf(eventTarget: EventTarget | null): Element | null {
  return eventTarget instanceof Element ? eventTarget.closest(`[${TOOLTIP_ATTR}]`) : null;
}

function showNow(target: Element): void {
  cancelPending();
  const text = tooltipTextOf(target);
  if (text !== null) {
    showBubble(target, text, placementOf(target));
  }
}

function onPointerOver(event: Event): void {
  const target = triggerOf(event.target);
  if (target === null) {
    // Off every trigger: the pointer has left whatever was up or queued.
    dropTooltip();
    return;
  }
  // Only an entry into the trigger counts. ``mouseover`` also fires for boundaries
  // WITHIN one trigger (its icon and its own padding are two elements), and a
  // dismissal -- a click above all -- clears ``shownFor`` and ``pendingFor``, so
  // without this a dismissed bubble comes back on a twitch that never left.
  const from = event instanceof MouseEvent ? event.relatedTarget : null;
  if (from instanceof Node && target.contains(from)) {
    return;
  }
  if (target === shownFor || target === pendingFor) {
    return;
  }
  cancelPending();
  hideBubble();
  pendingFor = target;
  pendingTimer = window.setTimeout(() => showNow(target), TOOLTIP_DELAY_MS);
}

function onPointerOut(event: Event): void {
  const target = triggerOf(event.target);
  if (target === null) {
    return;
  }
  // Moving within one trigger's own subtree is not leaving it.
  const to = event instanceof MouseEvent ? event.relatedTarget : null;
  if (to instanceof Node && target.contains(to)) {
    return;
  }
  if (target === shownFor || target === pendingFor) {
    dropTooltip();
  }
}

// Keyboard focus only -- not focus that came from a mouse click, which would
// flash the tooltip and then immediately hide it on the click.
function onFocusIn(event: Event): void {
  const target = triggerOf(event.target);
  if (target !== null && target.matches(":focus-visible")) {
    showNow(target);
  }
}

// Only the tooltip's OWN trigger losing focus drops it, as with the pointer:
// ``focusout`` bubbles to the document, so an unrelated focus move (a rename
// editor opening, a dialog taking focus) would otherwise take down a bubble the
// pointer is still resting on.
function onFocusOut(event: Event): void {
  const target = triggerOf(event.target);
  if (target !== null && (target === shownFor || target === pendingFor)) {
    dropTooltip();
  }
}

function wireListeners(): void {
  // Views are also built without a browser (the chat app walks vnodes in plain
  // node tests, where `document` is a stub and there is no `window` at all),
  // and building one must not require either. Nothing can be hovered there, so
  // there is nothing to wire.
  if (isWired || typeof window === "undefined" || typeof document === "undefined") {
    return;
  }
  isWired = true;
  // One set of listeners for every tooltip in the workspace: the text lives on
  // the elements, so nothing here is per-trigger. ``mouseover``/``mouseout``
  // rather than enter/leave because only these bubble to the document.
  document.addEventListener("mouseover", onPointerOver);
  document.addEventListener("mouseout", onPointerOut);
  document.addEventListener("focusin", onFocusIn);
  document.addEventListener("focusout", onFocusOut);
  // Any click, wherever it lands: a press is the user acting rather than
  // reading, and the thing they pressed may well move what is underneath.
  document.addEventListener("click", dropTooltip, true);
  // Any scroll (capture, so nested scrollers count), resize or window blur
  // slides the trigger out from under a shown bubble, so drop it.
  window.addEventListener("scroll", dropTooltip, true);
  window.addEventListener("resize", dropTooltip);
  window.addEventListener("blur", dropTooltip);
}

/**
 * Give an element a tooltip, or take it away with ``null``. For DOM this
 * workspace builds by hand (the lightbox, the dock's tab strip); mithril views
 * spread ``hoverTooltipAttrs`` instead. Removing the element needs no cleanup:
 * nothing is attached to it.
 */
export function setHoverTooltip(element: Element, text: string | null, placement: TooltipPlacement = "below"): void {
  wireListeners();
  if (text === null) {
    element.removeAttribute(TOOLTIP_ATTR);
  } else {
    element.setAttribute(TOOLTIP_ATTR, text);
  }
  if (placement === "below") {
    element.removeAttribute(PLACEMENT_ATTR);
  } else {
    element.setAttribute(PLACEMENT_ATTR, placement);
  }
}

/**
 * The mithril form: spread into an element's attrs in place of a native
 * ``title``, e.g. ``m("button", { onclick, ...hoverTooltipAttrs("Close") })``.
 * The element keeps its own ``aria-label`` -- the bubble is decoration, not an
 * accessible name. ``placement`` defaults to the shared centered-below
 * behavior; pass ``"right"`` only for the rail's exception (see the module
 * doc comment).
 *
 * ``text`` is ``null`` for no tooltip, and so is dropping these attrs
 * altogether: both leave the element without the attribute, which is the whole
 * of the state. That is why the text lives in an attribute rather than in a
 * closure held by lifecycle hooks -- mithril patches the same element across
 * redraws and runs only the current vnode's hooks, so anything a vanished
 * spread left behind would have nothing to clean it up.
 */
export function hoverTooltipAttrs(text: string | null, placement: TooltipPlacement = "below"): m.Attributes {
  wireListeners();
  // Null values are what mithril removes an attribute for, on the update where
  // they appear -- so a caller passing null, and a caller dropping the spread,
  // land in the same place.
  return {
    [TOOLTIP_ATTR]: text,
    [PLACEMENT_ATTR]: placement === "below" ? null : placement,
  };
}
