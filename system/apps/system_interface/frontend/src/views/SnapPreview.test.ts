// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "../testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import type { PixelRect } from "../geometry/frames";
import { SnapPreview, applySnapPreviewStyle } from "./SnapPreview";

afterEach(unmountViews);

const ZONE: PixelRect = { x: 0, y: 0, width: 500, height: 800 };

function render(rect: PixelRect | null): HTMLElement {
  return mountView(() => m(SnapPreview, { rect })).querySelector("[data-snap-preview]") as HTMLElement;
}

describe("SnapPreview", () => {
  it("is in the DOM and hidden with no zone, and shown at the zone's rectangle with one", () => {
    const hidden = render(null);
    expect(hidden.style.display).toBe("none");
    unmountViews();
    const shown = render(ZONE);
    expect(shown.style.display).toBe("");
    expect(shown.style.left).toBe("0px");
    expect(shown.style.width).toBe("500px");
    expect(shown.style.height).toBe("800px");
  });

  it("paints the same style a render would, so a redraw mid-drag changes nothing", () => {
    const rendered = render(ZONE).style.cssText;
    unmountViews();
    const painted = render(null);
    applySnapPreviewStyle(painted, ZONE);
    expect(painted.style.cssText).toBe(rendered);
    const renderedHidden = render(null).style.cssText;
    applySnapPreviewStyle(painted, null);
    expect(painted.style.cssText).toBe(renderedHidden);
  });
});
