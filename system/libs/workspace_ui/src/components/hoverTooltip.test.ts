// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import m from "mithril";

import { hoverTooltipAttrs, placeTooltip, setHoverTooltip } from "./hoverTooltip";
import { hoverTooltip, hoverTooltipText, shownTooltipText, unhoverTooltip } from "../testing/tooltip";

const VIEWPORT = { width: 1000, height: 800 };
const BUBBLE = { width: 100, height: 20 };

describe("placeTooltip", () => {
  it("centers the bubble under the trigger with a 6px gap", () => {
    const anchor = { left: 400, top: 300, bottom: 320, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT)).toEqual({ left: 370, top: 326 });
  });

  it("flips above the trigger when the bubble would overflow the bottom", () => {
    const anchor = { left: 400, top: 760, bottom: 790, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT)).toEqual({ left: 370, top: 734 });
  });

  it("stays below when flipping above would not fit either", () => {
    // A trigger taller than the viewport: neither side has room, so the bubble
    // keeps its natural place under the trigger and only the edge clamp applies.
    const anchor = { left: 400, top: 2, bottom: 795, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT)).toEqual({ left: 370, top: 801 });
  });

  it("clamps to 6px from the left edge for a trigger against it", () => {
    const anchor = { left: 0, top: 300, bottom: 320, width: 20 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT).left).toBe(6);
  });

  it("clamps to 6px from the right edge for a trigger against it", () => {
    const anchor = { left: 980, top: 300, bottom: 320, width: 20 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT).left).toBe(894);
  });

  it("clamps to 6px from the top for a trigger scrolled off the top", () => {
    const anchor = { left: 400, top: -60, bottom: -30, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT).top).toBe(6);
  });

  it("prefers the left clamp when the bubble is wider than the viewport", () => {
    const anchor = { left: 400, top: 300, bottom: 320, width: 40 };
    expect(placeTooltip(anchor, { width: 1200, height: 20 }, VIEWPORT).left).toBe(6);
  });

  it('defaults to the same result as an explicit "below" placement', () => {
    const anchor = { left: 400, top: 300, bottom: 320, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT)).toEqual(placeTooltip(anchor, BUBBLE, VIEWPORT, "below"));
  });
});

describe("placeTooltip right placement", () => {
  it("sits beside the trigger with a 6px gap, vertically centered on it", () => {
    const anchor = { left: 400, top: 300, bottom: 320, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT, "right")).toEqual({ left: 446, top: 300 });
  });

  it("flips to the trigger's left when the bubble would overflow the right edge", () => {
    const anchor = { left: 950, top: 300, bottom: 320, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT, "right")).toEqual({ left: 844, top: 300 });
  });

  it("never runs off the right edge even when the left flip has nowhere to go either", () => {
    // A bubble wide enough that neither the trigger's right nor its left has
    // room: the right-hand position wins (as the primary side), but the
    // right-edge clamp still pulls it back the last two pixels so the bubble
    // ends flush with the margin instead of hanging off the viewport.
    const anchor = { left: 50, top: 300, bottom: 320, width: 40 };
    expect(placeTooltip(anchor, { width: 900, height: 20 }, VIEWPORT, "right")).toEqual({ left: 94, top: 300 });
  });

  it("clamps to 6px from the top for a trigger scrolled off the top", () => {
    const anchor = { left: 400, top: -10, bottom: 10, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT, "right")).toEqual({ left: 446, top: 6 });
  });

  it("clamps to the bottom margin for a trigger against the bottom edge", () => {
    // Unlike the "below" placement's flip axis, right-placement clamps its
    // perpendicular (vertical) axis on both sides, so a low trigger cannot
    // push the bubble past the viewport's bottom edge either.
    const anchor = { left: 400, top: 790, bottom: 810, width: 40 };
    expect(placeTooltip(anchor, BUBBLE, VIEWPORT, "right")).toEqual({ left: 446, top: 774 });
  });
});

describe("hoverTooltipAttrs", () => {
  // Attached to the document: the tooltip listens at the document, so a detached
  // tree never sees the hover.
  const root = document.createElement("div");
  document.body.appendChild(root);

  afterEach(() => {
    m.render(root, []);
    vi.useRealTimers();
  });

  function renderButton(text: string | null): HTMLElement {
    m.render(root, m("button", { ...hoverTooltipAttrs(text) }, m("span", "Go")));
    return root.firstElementChild as HTMLElement;
  }

  it("follows its text to and from null across redraws of the same element", () => {
    vi.useFakeTimers();
    const button = renderButton(null);
    expect(hoverTooltipText(button)).toBeNull();

    expect(renderButton("Start")).toBe(button);
    expect(hoverTooltipText(button)).toBe("Start");

    expect(renderButton(null)).toBe(button);
    expect(hoverTooltipText(button)).toBeNull();
  });

  it("drops the tooltip when a redraw stops spreading the attrs at all", () => {
    vi.useFakeTimers();
    const button = renderButton("Start");
    expect(hoverTooltipText(button)).toBe("Start");

    // The shape that used to strand a tooltip: a caller that switches to `{}`
    // rather than passing null. Mithril patches the element and runs only the
    // new vnode's hooks, so nothing gets a chance to clean up -- the text has
    // to live somewhere mithril itself diffs.
    m.render(root, m("button", {}, m("span", "Go")));
    expect(root.firstElementChild).toBe(button);
    expect(hoverTooltipText(button)).toBeNull();
  });

  it("picks up a tooltip an element gains only on a later redraw", () => {
    vi.useFakeTimers();
    m.render(root, m("button", {}, m("span", "Go")));
    const button = root.firstElementChild as HTMLElement;
    expect(hoverTooltipText(button)).toBeNull();

    // The mirror image: the element exists before it has anything to say, so
    // there is no create hook to attach anything.
    expect(renderButton("Start")).toBe(button);
    expect(hoverTooltipText(button)).toBe("Start");
  });

  it("answers a hover over anything inside the trigger", () => {
    vi.useFakeTimers();
    const button = renderButton("Start");
    expect(hoverTooltipText(button.querySelector("span")!)).toBe("Start");
  });

  it("stays down after a click until the pointer leaves the trigger", () => {
    vi.useFakeTimers();
    const button = renderButton("Start");
    const label = button.querySelector("span")!;
    hoverTooltip(button);
    expect(shownTooltipText()).toBe("Start");

    // A click dismisses the bubble, and the pointer is still on the button.
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(shownTooltipText()).toBeNull();

    // Crossing between the button and its own label is movement within the trigger,
    // not an entry into it -- the bubble must not come back without the pointer leaving.
    label.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, relatedTarget: button }));
    vi.runAllTimers();
    expect(shownTooltipText()).toBeNull();

    // Leaving and coming back does bring it back.
    expect(hoverTooltipText(button)).toBe("Start");
  });

  it("follows a text change on its trigger while the bubble is up", async () => {
    vi.useFakeTimers();
    const button = renderButton("Starting");
    hoverTooltip(button);
    expect(shownTooltipText()).toBe("Starting");

    // What the dock's status dot does when an app's status changes under a resting pointer.
    expect(renderButton("Running")).toBe(button);
    await Promise.resolve();
    expect(shownTooltipText()).toBe("Running");

    unhoverTooltip(button);
  });

  it("takes the bubble down with an element that leaves the document while it is up", async () => {
    vi.useFakeTimers();
    // Hand-built DOM, as the lightbox and the dock's tab strip build it.
    const button = document.createElement("button");
    document.body.appendChild(button);
    setHoverTooltip(button, "Download");
    hoverTooltip(button);
    expect(shownTooltipText()).toBe("Download");

    // Torn out from under the pointer, which fires no leave event of its own,
    // and -- the point of the change -- has no teardown call to forget either.
    button.remove();
    await Promise.resolve();
    expect(shownTooltipText()).toBeNull();
  });

  it("stops offering a tooltip an imperative caller takes back", () => {
    vi.useFakeTimers();
    const button = document.createElement("button");
    document.body.appendChild(button);
    setHoverTooltip(button, "Download");
    expect(hoverTooltipText(button)).toBe("Download");

    setHoverTooltip(button, null);
    expect(hoverTooltipText(button)).toBeNull();
    button.remove();
  });
});
