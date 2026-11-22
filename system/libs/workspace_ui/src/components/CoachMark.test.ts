// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import { placeCoachMark, showCoachMark } from "./CoachMark";

const VIEWPORT = { width: 1000, height: 800 };
const BUBBLE = { width: 200, height: 80 };

describe("placeCoachMark", () => {
  it("goes below the control, centred on it, with the arrow at the control's centre", () => {
    const anchor = { left: 480, top: 300, width: 40, height: 30 };
    expect(placeCoachMark(anchor, BUBBLE, VIEWPORT)).toEqual({ side: "below", left: 400, top: 340, arrowOffset: 100 });
  });

  it("goes above a control with no room under it", () => {
    const anchor = { left: 480, top: 740, width: 40, height: 30 };
    expect(placeCoachMark(anchor, BUBBLE, VIEWPORT)).toEqual({ side: "above", left: 400, top: 650, arrowOffset: 100 });
  });

  it("goes to the right of a control as tall as the viewport, level with its centre", () => {
    const anchor = { left: 100, top: 0, width: 40, height: 800 };
    expect(placeCoachMark(anchor, BUBBLE, VIEWPORT)).toEqual({ side: "right", left: 150, top: 360, arrowOffset: 40 });
  });

  it("goes to the left when the right has no room either", () => {
    const anchor = { left: 900, top: 0, width: 40, height: 800 };
    expect(placeCoachMark(anchor, BUBBLE, VIEWPORT)).toEqual({ side: "left", left: 690, top: 360, arrowOffset: 40 });
  });

  it("stays inside the viewport beside a control at its edge, the arrow still on the control", () => {
    // The send button of a composer in the window's bottom-right corner: no room below, so above, pulled in
    // from the right edge, with the arrow moved off the bubble's centre to stay over the button.
    const anchor = { left: 950, top: 740, width: 32, height: 32 };
    expect(placeCoachMark(anchor, BUBBLE, VIEWPORT)).toEqual({ side: "above", left: 792, top: 650, arrowOffset: 174 });
  });

  it("keeps the arrow off the bubble's corner for a control past its end", () => {
    const anchor = { left: 990, top: 300, width: 8, height: 20 };
    expect(placeCoachMark(anchor, BUBBLE, VIEWPORT).arrowOffset).toBe(186);
  });

  it("falls back to below, held inside the viewport, when no side has room", () => {
    const anchor = { left: 0, top: 0, width: 1000, height: 800 };
    expect(placeCoachMark(anchor, BUBBLE, VIEWPORT)).toEqual({ side: "below", left: 400, top: 712, arrowOffset: 100 });
  });
});

function nextFrame(): Promise<void> {
  return new Promise((resolve) => requestAnimationFrame(() => resolve()));
}

/** Let a mutation the page made reach the observer, and the reposition it schedules run. */
async function settle(): Promise<void> {
  await Promise.resolve();
  await nextFrame();
}

function mountAnchor(): HTMLButtonElement {
  const anchor = document.createElement("button");
  document.body.appendChild(anchor);
  return anchor;
}

function bubble(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".coach-mark");
}

describe("showCoachMark", () => {
  afterEach(() => {
    document.body.replaceChildren();
  });

  it("puts up a bubble with the text, an arrow and a Got it button", () => {
    const handle = showCoachMark({ anchor: mountAnchor(), text: "Click this button to send your message" });

    expect(bubble()?.querySelector(".coach-mark-text")?.textContent).toBe("Click this button to send your message");
    expect(bubble()?.querySelector(".coach-mark-dismiss")?.textContent).toBe("Got it");
    expect(bubble()?.querySelector(".coach-mark-arrow")).not.toBeNull();
    expect(bubble()?.dataset.side).toBe("below");
    handle.dismiss();
  });

  it("goes when Got it is pressed, and says so once", () => {
    const onDismiss = vi.fn();
    const handle = showCoachMark({ anchor: mountAnchor(), text: "Here", onDismiss });

    bubble()?.querySelector<HTMLButtonElement>(".coach-mark-dismiss")?.click();
    handle.dismiss();

    expect(bubble()).toBeNull();
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("goes when the control it points at is clicked", () => {
    const onDismiss = vi.fn();
    const anchor = mountAnchor();
    showCoachMark({ anchor, text: "Here", onDismiss });

    anchor.click();

    expect(bubble()).toBeNull();
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("goes when the control leaves the page", async () => {
    const onDismiss = vi.fn();
    const anchor = mountAnchor();
    showCoachMark({ anchor, text: "Here", onDismiss });

    anchor.remove();
    await settle();

    expect(bubble()).toBeNull();
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("stays up through redraws of the page that leave the control in it", async () => {
    const anchor = mountAnchor();
    const handle = showCoachMark({ anchor, text: "Here" });

    document.body.appendChild(document.createElement("p"));
    anchor.classList.add("moved");
    await settle();

    expect(bubble()).not.toBeNull();
    handle.dismiss();
  });

  it("has the control wear the pulse class only while it is up", () => {
    const anchor = mountAnchor();
    const handle = showCoachMark({ anchor, text: "Here", pulseClass: "coach-pulse" });
    expect(anchor.classList.contains("coach-pulse")).toBe(true);

    handle.dismiss();

    expect(anchor.classList.contains("coach-pulse")).toBe(false);
  });

  it("takes down the one that is up when another is shown", () => {
    const firstDismissed = vi.fn();
    showCoachMark({ anchor: mountAnchor(), text: "First", onDismiss: firstDismissed });

    const second = showCoachMark({ anchor: mountAnchor(), text: "Second" });

    expect(firstDismissed).toHaveBeenCalledTimes(1);
    expect(document.querySelectorAll(".coach-mark")).toHaveLength(1);
    expect(bubble()?.querySelector(".coach-mark-text")?.textContent).toBe("Second");
    second.dismiss();
  });
});
