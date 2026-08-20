// @vitest-environment jsdom
//
// The banner keys entirely off a meta tag the backend injects into the app
// shell, so these tests need a real document to plant that tag in.
import { afterEach, describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws; ensure one exists before the import below.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import m from "mithril";

import { STALENESS_MESSAGES, UpdateStalenessBanner, getUpdateStalenessVariant } from "./UpdateStalenessBanner";

const META_TAG_NAME = "system-interface-update-staleness";

function plantMetaTag(content: string): void {
  const tag = document.createElement("meta");
  tag.setAttribute("name", META_TAG_NAME);
  tag.setAttribute("content", content);
  document.head.appendChild(tag);
}

/** Mounts a fresh banner into a detached root, driven with plain `m.render`
 *  (not `m.mount`'s RAF-scheduled auto-redraw) so the dismiss click's state
 *  change lands synchronously on the next explicit redraw. */
function mountBanner(): { root: HTMLElement; redraw: () => void } {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const component = UpdateStalenessBanner();
  const redraw = (): void => {
    m.render(root, m(component));
  };
  redraw();
  return { root, redraw };
}

function bannerIn(root: HTMLElement): Element | null {
  return root.querySelector(".update-staleness-banner");
}

afterEach(() => {
  document.querySelectorAll(`meta[name="${META_TAG_NAME}"]`).forEach((tag) => tag.remove());
  document.body.innerHTML = "";
});

describe("getUpdateStalenessVariant", () => {
  it("reads the meta tag's content, and reads absence as empty", () => {
    expect(getUpdateStalenessVariant()).toBe("");
    plantMetaTag("update-interrupted");
    expect(getUpdateStalenessVariant()).toBe("update-interrupted");
  });
});

describe("UpdateStalenessBanner", () => {
  it("renders nothing when the shell carries no tag", () => {
    const { root } = mountBanner();
    expect(bannerIn(root)).toBeNull();
  });

  it("renders nothing for a variant it does not know", () => {
    // A newer backend's variant must degrade to no banner, not a blank one.
    plantMetaTag("some-future-variant");
    const { root } = mountBanner();
    expect(bannerIn(root)).toBeNull();
  });

  it.each(Object.keys(STALENESS_MESSAGES))("renders the %s message", (variant) => {
    plantMetaTag(variant);
    const { root } = mountBanner();
    expect(bannerIn(root)?.textContent).toContain(STALENESS_MESSAGES[variant]);
  });

  it("hides for the rest of the page load once dismissed", () => {
    plantMetaTag("updated-not-activated");
    const { root, redraw } = mountBanner();
    const button = root.querySelector(".update-staleness-banner-btn");
    if (button === null) throw new Error("no dismiss button rendered");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    redraw();
    expect(bannerIn(root)).toBeNull();
  });
});
