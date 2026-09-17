/**
 * Reading a hover tooltip in a view test. The workspace's tooltip is one shared bubble on
 * <body>, put up after a hover-intent delay and hidden inline, so a test reaches it through the
 * clock and the bubble's own styling rather than through the element it belongs to. It lives
 * beside `hoverTooltip.ts` because the marker class and the inline styling it reads are that
 * module's own, and every app's view tests read them the same way.
 */
import { vi } from "vitest";

/** Point at an element, as a real pointer does: the bubbling event the tooltip listens for and
 *  the element-local one, so a test does not encode which of them drives it. Needs fake timers --
 *  it runs out the hover-intent delay. */
export function hoverTooltip(element: HTMLElement): void {
  element.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, relatedTarget: document.body }));
  element.dispatchEvent(new MouseEvent("mouseenter", { relatedTarget: document.body }));
  vi.runAllTimers();
}

/** Point the pointer away again -- onto ``document.body``, not out of the window. A ``mouseout``
 *  whose ``relatedTarget`` is null is the browser's signal that the pointer left the window
 *  entirely, and views act on it (the project rail collapses). */
export function unhoverTooltip(element: HTMLElement): void {
  element.dispatchEvent(new MouseEvent("mouseout", { bubbles: true, relatedTarget: document.body }));
  element.dispatchEvent(new MouseEvent("mouseleave", { relatedTarget: document.body }));
}

/** The text of the bubble that is up right now, or null when none is. */
export function shownTooltipText(): string | null {
  const bubble = document.querySelector<HTMLElement>(".hover-tooltip");
  if (bubble === null || bubble.style.visibility !== "visible" || bubble.style.display === "none") {
    return null;
  }
  return bubble.textContent;
}

/** Hover an element, read what it says, and leave. */
export function hoverTooltipText(element: HTMLElement): string | null {
  hoverTooltip(element);
  const text = shownTooltipText();
  unhoverTooltip(element);
  return text;
}
