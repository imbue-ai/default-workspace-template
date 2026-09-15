/**
 * Reading a hover tooltip in a view test. The workspace's tooltip is one shared bubble on
 * <body>, put up after a hover-intent delay and hidden inline, so a test reaches it through the
 * clock and the bubble's own styling rather than through the element it belongs to.
 */
import { vi } from "vitest";

/** Point at an element past the tooltip's hover-intent delay (needs fake timers), and return the
 *  text of the bubble that came up, or null when none did. */
export function hoverTooltipText(element: HTMLElement): string | null {
  element.dispatchEvent(new MouseEvent("mouseenter"));
  vi.runAllTimers();
  const bubble = document.querySelector<HTMLElement>(".hover-tooltip");
  const text =
    bubble !== null && bubble.style.visibility === "visible" && bubble.style.display !== "none"
      ? bubble.textContent
      : null;
  element.dispatchEvent(new MouseEvent("mouseleave"));
  return text;
}
